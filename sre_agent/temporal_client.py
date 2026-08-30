#!/usr/bin/env python3
"""
Temporal client bootstrap for the code-fix verification sandbox.

Temporal here has exactly one job: run `sandbox_workflow.CodeFixVerificationWorkflow`,
which replays the log evidence that proved an incident was broken, applies a
proposed code patch inside an isolated K8s Job, re-runs, and diffs the logs to
answer "did this actually restore the previous healthy state?" It is not a
general task queue and nothing else should be scheduled on it.

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
        _CLIENT = await Client.connect(temporal_host(), namespace=temporal_namespace())
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
