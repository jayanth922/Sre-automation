#!/usr/bin/env python3
"""
Durable checkpointing for long-running investigations (interview Q2).

Agent investigations are long-running tasks (a gnarly incident can take many
minutes of back-and-forth with the LLM). If the process crashes mid-run, we do
not want to lose the work. LangGraph supports this natively via a *checkpointer*:
graph state is persisted per ``thread_id`` after every node, so a restarted
process can resume the same investigation from the last checkpoint.

This module provides:
- `get_checkpointer()` — build the configured checkpointer (or None when off).
- `thread_config(thread_id, base)` — inject the required ``thread_id`` into an
  invoke config, but only when checkpointing is enabled (so the default path is
  byte-for-byte unchanged and needs no thread_id).

Backends (via `CHECKPOINTER_BACKEND`):
- `memory` (default) — in-process; resumes within a running process.
- `redis` / `postgres` — external stores → true cross-crash durability (require
  the `langgraph-checkpoint-redis` / `langgraph-checkpoint-postgres` package).

Everything is gated by `CHECKPOINTER_ENABLED` (default false): when off,
`get_checkpointer()` returns None and `compile(checkpointer=None)` reproduces the
prior behavior exactly.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
_OPEN_CHECKPOINTER_CONTEXTS = []


def checkpointer_enabled() -> bool:
    return os.getenv("CHECKPOINTER_ENABLED", "false").lower() in ("true", "1", "yes")


def select_backend() -> str:
    return os.getenv("CHECKPOINTER_BACKEND", "memory").lower()


def durable_checkpointer_configured() -> bool:
    return checkpointer_enabled() and select_backend() in {"redis", "postgres"}


def _production_runtime() -> bool:
    environment = (
        os.getenv("SENTINEL_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or ""
    ).lower()
    return os.getenv("AGENT_MODE", "").lower() == "api" or environment in {
        "prod",
        "production",
    }


def _memory_saver():
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


async def _redis_saver():
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    url = os.getenv("CHECKPOINTER_REDIS_URL") or os.getenv("REDIS_URL", "redis://redis:6379")
    cm = AsyncRedisSaver.from_conn_string(url)
    saver = await cm.__aenter__()  # hold open for the process lifetime
    setup = getattr(saver, "asetup", None) or getattr(saver, "setup", None)
    if setup:
        result = setup()
        if inspect.isawaitable(result):
            await result
    _OPEN_CHECKPOINTER_CONTEXTS.append(cm)
    return saver


async def _postgres_saver():
    # Async graph execution requires the async saver methods (aget/aput/alist).
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    uri = (
        os.getenv("CHECKPOINTER_POSTGRES_URI")
        or os.getenv("DATABASE_URL")
        or _build_pg_uri()
    ).replace("postgresql+asyncpg://", "postgresql://", 1)
    cm = AsyncPostgresSaver.from_conn_string(uri)
    saver = await cm.__aenter__()  # hold open for the process lifetime
    await saver.setup()
    _OPEN_CHECKPOINTER_CONTEXTS.append(cm)
    return saver


def _build_pg_uri() -> str:
    user = os.getenv("POSTGRES_USER", "postgres")
    pw = os.getenv("POSTGRES_PASSWORD", "postgres")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "sre")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


async def get_checkpointer():
    """Return the configured checkpointer, or None when checkpointing is off.

    External backends fall back to memory only in local development.
    API/production runtimes fail closed because a memory fallback would make
    approval interrupts look durable while losing them on restart.
    """
    if not checkpointer_enabled():
        return None

    backend = select_backend()
    if backend not in {"memory", "redis", "postgres"}:
        if _production_runtime():
            raise RuntimeError("Unsupported checkpointer backend")
        logger.warning("Unknown checkpointer backend '%s'; using memory", backend)
        return _memory_saver()
    try:
        if backend == "redis":
            return await _redis_saver()
        if backend == "postgres":
            return await _postgres_saver()
    except Exception as e:
        if backend in {"redis", "postgres"} and _production_runtime():
            logger.error(
                "Configured durable checkpointer backend '%s' is unavailable; refusing memory fallback",
                backend,
            )
            raise RuntimeError("Configured durable checkpointer is unavailable") from e
        logger.warning(
            f"Checkpointer backend '{backend}' unavailable ({e}); "
            f"falling back to in-memory MemorySaver (not crash-durable)."
        )
    return _memory_saver()


def thread_id_from_state(state: Any) -> str:
    """Derive a stable checkpoint thread id from graph state.

    Prefers incident_id (so a resumed run rejoins the same investigation), then
    session_id, with a safe fallback.
    """
    if not isinstance(state, dict):
        return "adhoc"
    md = state.get("metadata", {}) or {}
    return str(state.get("incident_id") or md.get("incident_id") or state.get("session_id") or "adhoc")


def thread_config(thread_id: str, base: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Merge the required ``thread_id`` into an invoke config — only when enabled.

    When checkpointing is off, returns ``base`` unchanged (may be None), so
    existing invoke sites behave exactly as before. When on, adds
    ``configurable.thread_id`` so the checkpointer persists/resumes per thread.
    """
    # Always attach agent tracing (Langfuse) when configured, so every LLM /
    # tool / chain span is traced with tokens, cost, latency and the reasoning
    # trajectory. No-op when tracing is off. This is what makes observability
    # first-class at every graph invocation site.
    try:
        from .tracing import tracing_callbacks
    except ImportError:  # direct-file unit-test loading has no package context
        from sre_agent.tracing import tracing_callbacks

    cfg = tracing_callbacks(base)
    metadata = cfg.get("metadata", {}) if isinstance(cfg, dict) else {}
    if metadata.get("root_trace_id"):
        try:
            from .trace_evidence import trace_callbacks
        except ImportError:
            from sre_agent.trace_evidence import trace_callbacks

        cfg = trace_callbacks(cfg)

    if not checkpointer_enabled():
        return cfg
    cfg = dict(cfg or {})
    configurable = dict(cfg.get("configurable", {}))
    configurable["thread_id"] = thread_id
    cfg["configurable"] = configurable
    return cfg
