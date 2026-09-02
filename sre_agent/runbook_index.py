#!/usr/bin/env python3
"""Semantic (vector) search over generated runbook content.

`edge_mcp_servers/mcp_servers/runbooks_notion/server.py::search_runbooks` is a
plain keyword/token-overlap scorer over Notion page properties (see
`_score_record` there) — it has no embeddings and will miss a conceptually
related runbook that doesn't share vocabulary with the query (e.g. "checkout
pods crashlooping" won't match a runbook titled "OOMKilled remediation for
payment-service"). That tool can't be upgraded to do real vector search
in place: it ships as a thin, standalone customer-facing MCP container that
deliberately never imports `sre_agent` (see `sre_agent/embedding.py`'s
docstring), so it can't share this package's fastembed/Qdrant stack without
bundling it into a customer-facing image.

This module is the fix scoped to what `sre_agent` itself controls: a second,
additive retrieval path — using the same shared embedding singleton and
Qdrant instance as `sre_agent/memory_store.py` — that the Planner queries
directly (not via MCP) alongside the existing keyword tool. Runbooks are
indexed here at generation time, in `runbook_generator.py::_publish_to_notion`,
so the index only covers auto-generated runbooks (this doesn't back-fill
runbooks that already existed in a cluster's Notion database before this was
added).
"""

import logging
import os
import threading
import uuid
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

logger = logging.getLogger(__name__)

RUNBOOKS_COLLECTION = "sre_runbooks_v1"

# Distinct from memory_store's namespace so the same raw id string (unlikely
# to collide in practice, but the collections are conceptually different
# entities) never maps to the same point id across collections.
_POINT_ID_NAMESPACE = uuid.UUID("b1e9a7f0-3c2d-4e5a-9f7c-0d8b6a4e2c1f")


def _point_id(runbook_id: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, runbook_id))


class RunbookIndex:
    """Qdrant-based semantic index over generated runbook content."""

    def __init__(
        self,
        qdrant_url: Optional[str] = None,
        collection_name: str = RUNBOOKS_COLLECTION,
    ):
        self.collection_name = collection_name
        self.client = None
        self.embedding_available = False

        if not QDRANT_AVAILABLE:
            logger.warning("⚠️ Qdrant dependencies not installed. Runbook index will not work.")
            return

        if not qdrant_url:
            qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")

        try:
            self.client = QdrantClient(url=qdrant_url, timeout=10)
            self.client.get_collections()
            logger.info(f"✅ Connected to Qdrant at {qdrant_url}")

            embed_text("startup healthcheck")
            self.embedding_available = True

            self._ensure_collection()
        except Exception as e:
            logger.error(f"❌ Failed to initialize Qdrant runbook index: {e}")
            self.client = None

    def _ensure_collection(self):
        if not self.client:
            return
        try:
            collections = self.client.get_collections()
            collection_names = [c.name for c in collections.collections]
            if self.collection_name not in collection_names:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
                )
                logger.info(f"✅ Created collection: {self.collection_name}")
            else:
                logger.info(f"✅ Collection exists: {self.collection_name}")
        except Exception as e:
            logger.error(f"❌ Failed to ensure collection: {e}")

    def is_available(self) -> bool:
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

    def index_runbook(
        self,
        runbook_id: str,
        *,
        title: str,
        content: str,
        service: str = "",
        incident_type: str = "",
        severity: str = "",
        url: Optional[str] = None,
        organization_id: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ) -> bool:
        """Embed and upsert one runbook. Idempotent — re-indexing the same
        runbook_id (e.g. on regeneration) overwrites its existing point."""
        if not self.is_available():
            logger.warning("⚠️ Runbook index not available, cannot index runbook")
            return False

        try:
            vector = embed_text(f"{title}\n\n{content}")

            payload: Dict[str, Any] = {
                "runbook_id": runbook_id,
                "title": title,
                "content": content,
                "service": service,
                "incident_type": incident_type,
                "severity": severity,
                "url": url,
            }
            if organization_id is not None:
                payload["organization_id"] = organization_id
            if cluster_id is not None:
                payload["cluster_id"] = cluster_id

            self.client.upsert(
                collection_name=self.collection_name,
                points=[PointStruct(id=_point_id(runbook_id), vector=vector, payload=payload)],
            )
            logger.info(f"✅ Indexed runbook: {runbook_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to index runbook {runbook_id}: {e}")
            return False

    def search(
        self,
        query_text: str,
        limit: int = 5,
        score_threshold: float = 0.5,
        *,
        organization_id: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic search over indexed runbooks. Callers with a known tenant
        MUST pass organization_id/cluster_id to avoid leaking another
        tenant's runbooks."""
        if not self.is_available():
            logger.warning("⚠️ Runbook index not available, cannot search")
            return []

        try:
            query_embedding = embed_text(query_text)
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                limit=limit,
                score_threshold=score_threshold,
                query_filter=self._tenant_filter(organization_id, cluster_id),
            )
            return [
                {
                    "runbook_id": point.payload.get("runbook_id", "unknown"),
                    "title": point.payload.get("title", ""),
                    "content": point.payload.get("content", ""),
                    "service": point.payload.get("service", ""),
                    "incident_type": point.payload.get("incident_type", ""),
                    "severity": point.payload.get("severity", ""),
                    "url": point.payload.get("url"),
                    "similarity_score": point.score,
                }
                for point in response.points
            ]
        except Exception as e:
            logger.error(f"❌ Failed to search runbook index: {e}")
            return []


def format_runbooks_for_prompt(runbooks: List[Dict[str, Any]]) -> str:
    if not runbooks:
        return ""
    formatted = "### RELEVANT RUNBOOK EVIDENCE (semantic match)\n\n"
    for i, rb in enumerate(runbooks, 1):
        formatted += f"#### Runbook {i}: {rb['title']} (similarity: {rb['similarity_score']:.2%})\n"
        formatted += f"{rb['content']}\n\n---\n\n"
    return formatted


# Global instance
_runbook_index: Optional[RunbookIndex] = None
_runbook_index_lock = threading.Lock()


def get_runbook_index() -> RunbookIndex:
    """Get or create the global runbook index instance."""
    global _runbook_index
    if _runbook_index is None:
        with _runbook_index_lock:
            if _runbook_index is None:
                _runbook_index = RunbookIndex()
    return _runbook_index
