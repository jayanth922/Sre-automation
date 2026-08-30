#!/usr/bin/env python3
"""Shared text-embedding bootstrap for Sentinel's vector-backed stores.

Previously each Qdrant-backed store (memory_store.py, and separately
edge_mcp_servers/mcp_servers/runbooks_local/server.py) instantiated its own
`fastembed.TextEmbedding` model. This module is the single in-process
embedding singleton for everything that runs inside the sre_agent package —
memory_store.py uses it, and any future Qdrant-backed skill_store.py should
too.

edge_mcp_servers/mcp_servers/runbooks_local/server.py intentionally keeps its
own independent bootstrap: it ships as a standalone "customer MCP tool
server" container (see its Dockerfile) that copies only its own server.py
and never imports sre_agent, so it cannot depend on this module without
bundling the whole sre_agent package into a customer-facing image.
"""

import logging
import os
import threading
from typing import List, Optional

try:
    from fastembed import TextEmbedding
    FASTEMBED_AVAILABLE = True
except ImportError:
    FASTEMBED_AVAILABLE = False
    TextEmbedding = None

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = os.getenv("SENTINEL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM = 384  # bge-small-en-v1.5 output size

_model: Optional["TextEmbedding"] = None
_model_lock = threading.Lock()


def get_embedding_model() -> Optional["TextEmbedding"]:
    """Return the process-wide TextEmbedding instance, creating it on first use."""
    global _model
    if not FASTEMBED_AVAILABLE:
        return None
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = TextEmbedding(model_name=EMBEDDING_MODEL_NAME)
                logger.info("Initialized shared embedding model: %s", EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts, returning one vector per input in order."""
    model = get_embedding_model()
    if model is None:
        raise RuntimeError("fastembed is not installed; cannot generate embeddings")
    return [vector.tolist() for vector in model.embed(texts)]


def embed_text(text: str) -> List[float]:
    """Embed a single text string."""
    return embed_texts([text])[0]
