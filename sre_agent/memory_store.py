#!/usr/bin/env python3
"""
Qdrant Memory Store for SRE Agent

Provides long-term memory (RAG) for incident correlation and past solution
retrieval. Uses Qdrant vector database for similarity search.

Each incident is stored with three separately-embedded fields (symptoms,
root_cause, resolution) as named vectors rather than one flat blob, so a
query can match on the part of the incident it's actually asking about.
Search results also carry a recency-decayed score (older incidents count
less, tenant-scoped so decay tuning is global) and forward/back cross-links
to related past incidents, computed at store time.
"""

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .embedding import EMBEDDING_DIM, embed_text

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        VectorParams,
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Collection name for incidents. Bumped to v2 because the vector schema
# changed from one flat vector to three named vectors (symptoms/root_cause/
# resolution) — a v1 collection can't be reused in place for that.
INCIDENTS_COLLECTION = "sre_incidents_v2"

# The three separately-embedded fields every incident is stored and searched
# against.
INCIDENT_FIELDS = ("symptoms", "root_cause", "resolution")

# How similar a candidate must be (raw cosine score) to count as "related"
# for cross-incident back-linking.
RELATED_SCORE_THRESHOLD = 0.5

# Half-life, in days, for the recency decay applied on top of raw similarity
# when ranking search results. Configurable since how fast "stale" incidents
# should stop mattering is a product/ops tuning knob, not a constant.
RECENCY_HALF_LIFE_DAYS = float(os.getenv("SENTINEL_MEMORY_RECENCY_HALF_LIFE_DAYS", "30"))

# Stable namespace for deriving Qdrant point IDs from incident_id (uuid5 is
# deterministic across processes, unlike Python's hash() which is
# PYTHONHASHSEED-randomized per-process for strings — using hash() meant
# re-storing the same incident_id from a different process created a
# duplicate point instead of upserting the existing one).
_POINT_ID_NAMESPACE = uuid.UUID("6f9c7e1a-9b3a-4a3e-9b7e-8a5b0f2c9d1e")


def _point_id(incident_id: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, incident_id))


def _recency_decay(stored_at: Optional[str], now: datetime, half_life_days: float) -> float:
    """Exponential decay factor in (0, 1] based on incident age; 1.0 if unknown/disabled."""
    if not stored_at or half_life_days <= 0:
        return 1.0
    try:
        stored_dt = datetime.fromisoformat(stored_at)
    except ValueError:
        return 1.0
    if stored_dt.tzinfo is None:
        stored_dt = stored_dt.replace(tzinfo=timezone.utc)
    age_days = max((now - stored_dt).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / half_life_days)


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
        self.embedding_available = False

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

            # Confirm the shared embedding model can actually load. The model
            # itself is a lazily-created process-wide singleton owned by
            # sre_agent.embedding (shared with any other Qdrant-backed store
            # in this package), not per-instance state here.
            embed_text("startup healthcheck")
            self.embedding_available = True
            logger.info("✅ Embedding model available")

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
                # Create collection with one named vector per structured field
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        field: VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE)
                        for field in INCIDENT_FIELDS
                    },
                )
                logger.info(f"✅ Created collection: {self.collection_name}")
            else:
                logger.info(f"✅ Collection exists: {self.collection_name}")

        except Exception as e:
            logger.error(f"❌ Failed to ensure collection: {e}")

    def is_available(self) -> bool:
        """Check if memory store is available."""
        return self.client is not None and self.embedding_available

    @staticmethod
    def _tenant_filter(
        organization_id: Optional[str], cluster_id: Optional[str]
    ) -> Optional["Filter"]:
        conditions = []
        if organization_id is not None:
            conditions.append(
                FieldCondition(key="organization_id", match=MatchValue(value=organization_id))
            )
        if cluster_id is not None:
            conditions.append(
                FieldCondition(key="cluster_id", match=MatchValue(value=cluster_id))
            )
        return Filter(must=conditions) if conditions else None

    def _find_related_incident_ids(
        self,
        *,
        root_cause: str,
        organization_id: Optional[str],
        cluster_id: Optional[str],
        exclude_incident_id: str,
        limit: int,
    ) -> List[str]:
        """Find past incidents with a similar root cause, for cross-linking."""
        if limit <= 0:
            return []
        try:
            query_embedding = embed_text(root_cause)
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                using="root_cause",
                limit=limit,
                score_threshold=RELATED_SCORE_THRESHOLD,
                query_filter=self._tenant_filter(organization_id, cluster_id),
            )
            return [
                point.payload.get("incident_id")
                for point in response.points
                if point.payload.get("incident_id")
                and point.payload.get("incident_id") != exclude_incident_id
            ]
        except Exception as e:
            logger.warning(
                "⚠️ Failed to compute related incidents for %s: %s", exclude_incident_id, e
            )
            return []

    def _backlink_related_incidents(
        self, related_incident_ids: List[str], new_incident_id: str
    ) -> None:
        """Add new_incident_id to each related incident's own related_incident_ids."""
        for related_id in related_incident_ids:
            try:
                point_id = _point_id(related_id)
                records = self.client.retrieve(
                    collection_name=self.collection_name,
                    ids=[point_id],
                    with_payload=True,
                )
                if not records:
                    continue
                current_links = set(records[0].payload.get("related_incident_ids") or [])
                if new_incident_id in current_links:
                    continue
                current_links.add(new_incident_id)
                self.client.set_payload(
                    collection_name=self.collection_name,
                    payload={"related_incident_ids": sorted(current_links)},
                    points=[point_id],
                )
            except Exception as e:
                logger.warning(
                    "⚠️ Failed to back-link incident %s -> %s: %s",
                    new_incident_id,
                    related_id,
                    e,
                )

    def store_incident(
        self,
        incident_id: str,
        *,
        symptoms: str,
        root_cause: str,
        resolution: str,
        metadata: Optional[Dict[str, Any]] = None,
        organization_id: Optional[str] = None,
        cluster_id: Optional[str] = None,
        related_limit: int = 3,
    ) -> bool:
        """
        Store an incident in the memory store as three separately-embedded
        fields, with recency and cross-incident back-links.

        Args:
            incident_id: Unique identifier for the incident
            symptoms: What was observed (alert context, evidence)
            root_cause: The diagnosed or hypothesized root cause
            resolution: What fixed it
            metadata: Additional metadata (alert_name, resolution, etc.)
            organization_id: Tenant scoping key, written into the payload so
                search_similar_incidents() can filter cross-tenant leakage.
            cluster_id: Cluster scoping key, same purpose as organization_id.
            related_limit: Max number of past incidents to cross-link against,
                by root_cause similarity. 0 disables back-linking.

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            logger.warning("⚠️ Memory store not available, cannot store incident")
            return False

        try:
            vectors = {
                "symptoms": embed_text(symptoms),
                "root_cause": embed_text(root_cause),
                "resolution": embed_text(resolution),
            }

            related_incident_ids = self._find_related_incident_ids(
                root_cause=root_cause,
                organization_id=organization_id,
                cluster_id=cluster_id,
                exclude_incident_id=incident_id,
                limit=related_limit,
            )

            payload = dict(metadata or {})
            payload["incident_id"] = incident_id
            payload["symptoms"] = symptoms
            payload["root_cause"] = root_cause
            payload["resolution"] = resolution
            payload["incident_text"] = (
                f"Symptoms: {symptoms}\n\nRoot Cause: {root_cause}\n\nResolution: {resolution}"
            )
            payload.setdefault("stored_at", datetime.now(timezone.utc).isoformat())
            payload["related_incident_ids"] = related_incident_ids
            if organization_id is not None:
                payload["organization_id"] = organization_id
            if cluster_id is not None:
                payload["cluster_id"] = cluster_id

            self.client.upsert(
                collection_name=self.collection_name,
                points=[PointStruct(id=_point_id(incident_id), vector=vectors, payload=payload)],
            )

            self._backlink_related_incidents(related_incident_ids, incident_id)

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
        *,
        organization_id: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for similar past incidents across all three structured fields
        (symptoms/root_cause/resolution), ranked by similarity decayed by
        recency.

        Args:
            query_text: Query text (e.g., alert context, incident description)
            limit: Maximum number of results
            score_threshold: Minimum raw similarity score (0.0-1.0) on
                whichever field matched best. Recency decay is applied after
                this threshold, as a re-ranking signal, not a second filter.
            organization_id: If set, restrict results to incidents stored with
                this organization_id. Callers with a known tenant MUST pass
                this to avoid leaking another tenant's incident history.
            cluster_id: If set, restrict results to incidents stored with this
                cluster_id.

        Returns:
            List of similar incidents with metadata, sorted by recency-decayed
            score (highest first)
        """
        if not self.is_available():
            logger.warning("⚠️ Memory store not available, cannot search incidents")
            return []

        try:
            query_embedding = embed_text(query_text)
            query_filter = self._tenant_filter(organization_id, cluster_id)

            # Query each structured field independently and keep the best
            # raw score per incident_id — an incident should surface whether
            # the query matches its symptoms, its root cause, or its fix.
            best_by_incident: Dict[str, Any] = {}
            for field in INCIDENT_FIELDS:
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_embedding,
                    using=field,
                    limit=limit,
                    score_threshold=score_threshold,
                    query_filter=query_filter,
                )
                for point in response.points:
                    incident_id = point.payload.get("incident_id", "unknown")
                    existing = best_by_incident.get(incident_id)
                    if existing is None or point.score > existing.score:
                        best_by_incident[incident_id] = point

            now = datetime.now(timezone.utc)
            ranked = sorted(
                best_by_incident.values(),
                key=lambda point: point.score
                * _recency_decay(point.payload.get("stored_at"), now, RECENCY_HALF_LIFE_DAYS),
                reverse=True,
            )[:limit]

            results = []
            for point in ranked:
                results.append({
                    "incident_id": point.payload.get("incident_id", "unknown"),
                    "incident_text": point.payload.get("incident_text", ""),
                    "similarity_score": point.score,
                    "related_incident_ids": point.payload.get("related_incident_ids", []),
                    "metadata": {
                        k: v
                        for k, v in point.payload.items()
                        if k not in ("incident_id", "incident_text")
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
            if incident.get("related_incident_ids"):
                formatted += (
                    f"**Related Incidents**: {', '.join(incident['related_incident_ids'])}\n\n"
                )
            formatted += "---\n\n"

        return formatted


# Global instance
_memory_store: Optional[MemoryStore] = None
_memory_store_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    """Get or create the global memory store instance."""
    global _memory_store
    if _memory_store is None:
        with _memory_store_lock:
            if _memory_store is None:
                _memory_store = MemoryStore()
    return _memory_store
