#!/usr/bin/env python3
"""
Temporal client bootstrap for the code-fix verification sandbox.

Temporal here has exactly one job: run `sandbox_workflow.CodeFixVerificationWorkflow`,
which replays the log evidence that proved an incident was broken, applies a
proposed code patch inside an isolated K8s Job, re-runs, and diffs the logs to
answer "did this actually restore the previous healthy state?" It is not a
general task queue and nothing else should be scheduled on it.

Two ways to run the Temporal server this talks to, picked purely by env vars
(no code change either way):
  - Local dev server (`platform/docker-compose.yaml`'s optional "temporal"
    service, `temporalio/temporal:latest server start-dev` — a single
    container with embedded SQLite, no Cassandra/MySQL/Elasticsearch cluster
    needed): leave TEMPORAL_API_KEY unset, TEMPORAL_HOST defaults to
    "temporal:7233" to match that service's compose hostname.
  - Temporal Cloud (managed, no local container): set TEMPORAL_HOST to your
    namespace's `<namespace>.<account>.tmprl.cloud:7233` endpoint and
    TEMPORAL_API_KEY to a Cloud API key — TLS is auto-enabled whenever an API
    key is present (Cloud requires it on the wire regardless of auth style).

Mirrors checkpointer.py's bootstrap shape: env-gated (`TEMPORAL_ENABLED`),
fails closed in production if configured but unreachable, degrades to a no-op
(logs a warning, returns None) only in non-production so local development
without a Temporal server doesn't break the OODA loop.

`start_workflow()` is fire-and-forget by design — the ACT phase must never
block on sandbox verification; the verdict lands later via
`incident_timeline.emit_timeline_event` from the worker process.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

_CLIENT: Optional[Any] = None

DEFAULT_TASK_QUEUE = "sentinel-sandbox"


def temporal_enabled() -> bool:
    return os.getenv("TEMPORAL_ENABLED", "false").lower() in ("true", "1", "yes")


def temporal_host() -> str:
    return os.getenv("TEMPORAL_HOST", "temporal:7233").strip()


def temporal_namespace() -> str:
    return os.getenv("TEMPORAL_NAMESPACE", "default").strip() or "default"


def task_queue() -> str:
    return os.getenv("TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE).strip() or DEFAULT_TASK_QUEUE


def temporal_api_key() -> Optional[str]:
    """Temporal Cloud API key. Unset for the local dev server (no auth)."""
    return os.getenv("TEMPORAL_API_KEY", "").strip() or None


def temporal_tls() -> bool:
    """Whether to connect over TLS.

    Auto-enabled whenever an API key is set, since Temporal Cloud requires TLS
    on the wire regardless of auth style — so TEMPORAL_HOST + TEMPORAL_API_KEY
    alone is enough to reach Cloud with zero other config. TEMPORAL_TLS, when
    set explicitly, always wins (e.g. a self-hosted server with TLS enabled
    but no API-key auth).
    """
    explicit = os.getenv("TEMPORAL_TLS")
    if explicit is not None:
        return explicit.strip().lower() in ("true", "1", "yes")
    return temporal_api_key() is not None


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


async def get_temporal_client() -> Optional[Any]:
    """Return a connected, cached Temporal client, or None when unavailable.

    Fails closed (raises) in production when Temporal is enabled but the
    server cannot be reached — a silently-dropped verification run would look
    like nothing was ever attempted. In non-production, connection failures
    degrade to None with a warning so local dev without a Temporal server
    keeps working.
    """
    global _CLIENT
    if not temporal_enabled():
        return None
    if _CLIENT is not None:
        return _CLIENT

    try:
        from temporalio.client import Client
    except ImportError as exc:
        if _production_runtime():
            raise RuntimeError(
                "TEMPORAL_ENABLED is set but the temporalio package is not installed"
            ) from exc
        logger.warning("temporalio is not installed; sandbox verification is disabled")
        return None

    try:
        _CLIENT = await Client.connect(
            temporal_host(),
            namespace=temporal_namespace(),
            api_key=temporal_api_key(),
            tls=temporal_tls(),
        )
    except Exception as exc:
        if _production_runtime():
            raise RuntimeError(
                f"Temporal is enabled but unreachable at {temporal_host()!r}"
            ) from exc
        logger.warning(
            "Temporal unreachable at %s (%s); sandbox verification is disabled",
            temporal_host(),
            exc,
        )
        return None
    return _CLIENT


async def start_workflow(
    workflow: Any,
    args: Sequence[Any],
    *,
    workflow_id: str,
    task_queue_name: Optional[str] = None,
) -> Optional[str]:
    """Fire-and-forget start of a workflow. Returns the workflow id, or None if
    Temporal is disabled/unavailable (never raises in that case — the caller's
    OODA loop must proceed regardless of sandbox verification's availability).
    """
    client = await get_temporal_client()
    if client is None:
        return None

    try:
        await client.start_workflow(
            workflow,
            args=list(args),
            id=workflow_id,
            task_queue=task_queue_name or task_queue(),
        )
    except Exception as exc:
        # A workflow that fails to *start* (as opposed to one that starts and
        # fails) must not take down the caller — this is always best-effort
        # from the ACT phase's perspective.
        logger.error("Failed to start Temporal workflow %s: %s", workflow_id, exc)
        return None
    return workflow_id


async def signal_workflow(
    workflow_id: str,
    signal_name: str,
    args: Sequence[Any] = (),
) -> bool:
    """Send a signal to a running workflow (Phase 5's two approval gates).

    Returns True if the signal was delivered, False if Temporal is
    disabled/unavailable or the workflow couldn't be signaled (e.g. it
    already completed or its approval window expired) — never raises. An
    approval endpoint must always return a clear result to the human
    clicking approve/deny regardless of the target workflow's state.
    """
    client = await get_temporal_client()
    if client is None:
        return False

    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(signal_name, *args)
    except Exception as exc:
        logger.error(
            "Failed to signal workflow %s (%s): %s", workflow_id, signal_name, exc
        )
        return False
    return True
