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

Enabled by default. Two credential modes:

- No cluster/org bound (local dev, self-hosted CLI runs —
  ``ExecutionContext.from_environment``): ``org_langfuse=None``.
  ``CallbackHandler()`` takes no backend-specific args in this mode — it
  reads LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY straight from
  the environment (see .env.example).
- A bound cluster/org (dashboard-managed multi-tenant runs): each org sets
  its own Langfuse project keys via Settings → Team (``Organization.
  langfuse_public_key``/``langfuse_secret_key``/``langfuse_host``, see
  ``sre_agent/api/v1/members.py::set_langfuse_config``). Passed in as
  ``org_langfuse={"public_key": ..., "secret_key": ..., "host": ...}``.
  Uses the SDK's (experimental) multi-project client registry — one
  ``Langfuse(...)`` client per public_key, looked up by
  ``CallbackHandler(public_key=...)`` — so two orgs' traces never land in
  the same project. An org that hasn't configured Langfuse gets no
  fallback to any operator-wide default project: its runs simply go
  untraced, so one tenant's trace data is never silently routed into
  another's (or the operator's own) project.

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

_DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"


def langfuse_enabled() -> bool:
    return os.getenv("LANGFUSE_TRACING", "true").lower() not in ("false", "0", "no")


def get_langfuse_callback(org_langfuse: Optional[Dict[str, Optional[str]]] = None) -> Optional[Any]:
    """Return a Langfuse LangChain CallbackHandler, or None if unavailable/off.

    ``org_langfuse`` distinguishes "no cluster/org at all" (``None`` — legacy
    env-var behavior) from "org exists but hasn't configured Langfuse" (a
    dict with missing/blank keys — no tracing, no env fallback).
    """
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler  # verified v3 import path

        if org_langfuse is None:
            return CallbackHandler()

        public_key = (org_langfuse.get("public_key") or "").strip()
        secret_key = (org_langfuse.get("secret_key") or "").strip()
        if not public_key or not secret_key:
            return None

        from langfuse import Langfuse

        Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=(org_langfuse.get("host") or "").strip() or _DEFAULT_LANGFUSE_HOST,
        )
        return CallbackHandler(public_key=public_key)
    except Exception as e:  # pragma: no cover - only without langfuse installed
        logger.warning(f"Langfuse tracing requested but unavailable ({e}); skipping.")
        return None


def tracing_callbacks(
    base: Optional[Dict[str, Any]] = None,
    org_langfuse: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Merge the Langfuse handler into an invoke ``config`` dict's callbacks list.

    Returns ``base`` unchanged when tracing is off, so wiring it in is a no-op by
    default. Pass the result straight to ``graph.astream(state, config=...)``.
    """
    handler = get_langfuse_callback(org_langfuse)
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
