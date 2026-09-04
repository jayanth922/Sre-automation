#!/usr/bin/env python3
"""
Langfuse tracing (competitive-audit upgrade #2: real LLM observability).

Swaps the in-process observability recorder for the industry-standard OSS tool.
Langfuse's LangChain integration is a callback handler: attach it to the graph's
invoke config and every LLM/chain/tool span is traced (latency, tokens, cost,
the reasoning trajectory). We keep the lightweight recorder for tests/offline;
this adds real tracing when Langfuse is configured.

Verified API (Langfuse Python SDK v3):
    from langfuse.langchain import CallbackHandler
    handler = CallbackHandler()
    graph.astream(state, config={"callbacks": [handler]})

Enabled by default. CallbackHandler() takes no backend-specific args — it
reads LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY from the
environment, so this talks identically to a self-hosted instance (what
docker-compose/Helm provision out of the box, see .env.example "Option A") or
to Langfuse Cloud (just point LANGFUSE_HOST at cloud.langfuse.com with that
project's keys, see .env.example "Option B" — no local containers needed).
Set LANGFUSE_TRACING=false to opt out. Guarded import so the module loads
without langfuse installed, and so a deployment with tracing nominally on but
no reachable Langfuse instance (e.g. bare local `uv run` with nothing
configured) degrades to a silent no-op rather than failing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def langfuse_enabled() -> bool:
    return os.getenv("LANGFUSE_TRACING", "true").lower() not in ("false", "0", "no")


def get_langfuse_callback() -> Optional[Any]:
    """Return a Langfuse LangChain CallbackHandler, or None if unavailable/off."""
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler  # verified v3 import path
        return CallbackHandler()
    except Exception as e:  # pragma: no cover - only without langfuse installed
        logger.warning(f"Langfuse tracing requested but unavailable ({e}); skipping.")
        return None


def tracing_callbacks(base: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Merge the Langfuse handler into an invoke ``config`` dict's callbacks list.

    Returns ``base`` unchanged when tracing is off, so wiring it in is a no-op by
    default. Pass the result straight to ``graph.astream(state, config=...)``.
    """
    handler = get_langfuse_callback()
    if handler is None:
        return base
    cfg: Dict[str, Any] = dict(base or {})
    callbacks: List[Any] = list(cfg.get("callbacks", []))
    callbacks.append(handler)
    cfg["callbacks"] = callbacks
    return cfg


def flush() -> None:
    """Flush pending traces (call on shutdown / after a run)."""
    if not langfuse_enabled():
        return
    try:
        from langfuse import get_client
        get_client().flush()
    except Exception as e:  # pragma: no cover
        logger.debug(f"Langfuse flush skipped: {e}")
