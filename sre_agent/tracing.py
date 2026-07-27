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

Enabled when LANGFUSE_PUBLIC_KEY is set (or LANGFUSE_TRACING=true). Guarded import
so the module loads without langfuse installed.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def langfuse_enabled() -> bool:
    if os.getenv("LANGFUSE_TRACING", "").lower() in ("true", "1", "yes"):
        return True
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY"))


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
