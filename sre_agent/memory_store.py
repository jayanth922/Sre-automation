#!/usr/bin/env python3
"""
Qdrant Memory Store for SRE Agent

Provides long-term memory (RAG) for incident correlation and past solution retrieval.
Uses Qdrant vector database for similarity search.
"""

import logging
import os
from typing import Any, Dict, List, Optional

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct
    from fastembed import TextEmbedding
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None
    TextEmbedding = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Collection name for incidents
INCIDENTS_COLLECTION = "sre_incidents"


class MemoryStore:
    """Qdrant-based memory store for incident correlation."""

    def __init__(
        self,
        qdrant_url: Optional[str] = None,
        collection_name: str = INCIDENTS_COLLECTION,
    ):
        """
        Initialize Qdrant memory store.

        Args:
            qdrant_url: Qdrant server URL (defaults to QDRANT_URL env var or localhost)
            collection_name: Name of the Qdrant collection
        """
        self.collection_name = collection_name
        self.client = None
        self.embedding_model = None

        if not QDRANT_AVAILABLE:
            logger.warning("⚠️ Qdrant dependencies not installed. Memory will not work.")
            logger.warning("⚠️ Install with: pip install qdrant-client fastembed")
            return

        # Get Qdrant URL
        if not qdrant_url:
            qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")

        try:
            # Initialize Qdrant client
            self.client = QdrantClient(url=qdrant_url, timeout=10)

            # Test connection
            self.client.get_collections()
            logger.info(f"✅ Connected to Qdrant at {qdrant_url}")

            # Initialize embedding model
            self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            logger.info("✅ Initialized embedding model")

            # Ensure collection exists
            self._ensure_collection()

        except Exception as e:
            logger.error(f"❌ Failed to initialize Qdrant memory store: {e}")
            self.client = None

    def _ensure_collection(self):
        """Ensure the incidents collection exists with proper configuration."""
        if not self.client:
            return

        try:
            # Check if collection exists
            collections = self.client.get_collections()
            collection_names = [c.name for c in collections.collections]

            if self.collection_name not in collection_names:
                # Create collection
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=384,  # bge-small-en-v1.5 embedding size
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(f"✅ Created collection: {self.collection_name}")
            else:
                logger.info(f"✅ Collection exists: {self.collection_name}")

        except Exception as e:
            logger.error(f"❌ Failed to ensure collection: {e}")

    def is_available(self) -> bool:
        """Check if memory store is available."""
        return self.client is not None and self.embedding_model is not None

    def store_incident(
        self,
        incident_text: str,
        incident_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Store an incident in the memory store.

        Args:
            incident_text: Text description of the incident and resolution
            incident_id: Unique identifier for the incident
            metadata: Additional metadata (alert_name, resolution, etc.)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            logger.warning("⚠️ Memory store not available, cannot store incident")
            return False

        try:
            # Generate embedding
            embeddings = list(self.embedding_model.embed([incident_text]))
            embedding = embeddings[0].tolist()

            # Prepare metadata
            payload = metadata or {}
            payload["incident_text"] = incident_text
            payload["incident_id"] = incident_id

            # Store in Qdrant
            point = PointStruct(
                id=hash(incident_id) % (2**63),  # Convert to int64
                vector=embedding,
                payload=payload,
            )

            self.client.upsert(
                collection_name=self.collection_name,
                points=[point],
            )

            logger.info(f"✅ Stored incident in memory: {incident_id}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to store incident: {e}")
            return False

    def search_similar_incidents(
        self,
        query_text: str,
        limit: int = 5,
        score_threshold: float = 0.7,
    ) -> List[Dict[str, Any]]:
        """
        Search for similar past incidents.

        Args:
            query_text: Query text (e.g., alert context, incident description)
            limit: Maximum number of results
            score_threshold: Minimum similarity score (0.0-1.0)

        Returns:
            List of similar incidents with metadata
        """
        if not self.is_available():
            logger.warning("⚠️ Memory store not available, cannot search incidents")
            return []

        try:
            # Generate query embedding
            embeddings = list(self.embedding_model.embed([query_text]))
            query_embedding = embeddings[0].tolist()

            # Search in Qdrant. qdrant-client >= 1.10 uses query_points;
            # the legacy `search()` method was removed.
            query_response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                limit=limit,
                score_threshold=score_threshold,
            )
            search_results = query_response.points

            # Format results
            results = []
            for result in search_results:
                results.append({
                    "incident_id": result.payload.get("incident_id", "unknown"),
                    "incident_text": result.payload.get("incident_text", ""),
                    "similarity_score": result.score,
                    "metadata": {
                        k: v
                        for k, v in result.payload.items()
                        if k not in ["incident_id", "incident_text"]
                    },
                })

            logger.info(f"✅ Found {len(results)} similar incidents")
            return results

        except Exception as e:
            logger.error(f"❌ Failed to search incidents: {e}")
            return []

    def format_similar_incidents_for_prompt(
        self, similar_incidents: List[Dict[str, Any]]
    ) -> str:
        """
        Format similar incidents for inclusion in LLM prompt.

        Args:
            similar_incidents: List of similar incidents from search

        Returns:
            Formatted string for prompt
        """
        if not similar_incidents:
            return "No similar past incidents found."

        formatted = "## Similar Past Incidents and Solutions:\n\n"
        for i, incident in enumerate(similar_incidents, 1):
            formatted += f"### Incident {i} (Similarity: {incident['similarity_score']:.2%})\n"
            formatted += f"**ID**: {incident['incident_id']}\n\n"
            formatted += f"**Description**: {incident['incident_text']}\n\n"
            if incident.get("metadata"):
                metadata = incident["metadata"]
                if "resolution" in metadata:
                    formatted += f"**Resolution**: {metadata['resolution']}\n\n"
            formatted += "---\n\n"

        return formatted


# Global instance
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """Get or create the global memory store instance."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
