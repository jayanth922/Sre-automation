#!/usr/bin/env python3
"""Background worker that claims and executes durable investigation leases."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import Any, Optional

from backend import database
from sre_agent.durable_jobs import (
    DurableJob,
    DurableJobError,
    default_lease_seconds,
)
from sre_agent.job_store import (
    claim_jobs,
    complete_job,
    fail_job,
    heartbeat_job,
)

logger = logging.getLogger(__name__)

_WORKER_TASK: Optional[asyncio.Task] = None
_STOP = asyncio.Event()


def _worker_id() -> str:
    configured = os.getenv("JOB_WORKER_ID", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _poll_interval() -> float:
    try:
        return max(0.2, float(os.getenv("JOB_WORKER_POLL_SECONDS", "2")))
    except ValueError:
        return 2.0


def _batch_size() -> int:
    try:
        return max(1, int(os.getenv("JOB_WORKER_BATCH_SIZE", "2")))
    except ValueError:
        return 2


async def execute_claimed_job(job: DurableJob, *, worker_id: str) -> None:
    """Run one claimed investigation job and finalize its lease."""
    from sre_agent.incident_runner import run_incident_investigation

    payload = job.payload
    handler = payload.get("handler")
    if handler != "run_graph_background_saas":
        raise DurableJobError(f"unsupported job handler: {handler}")

    incident_id = uuid.UUID(str(payload["incident_id"]))
    cluster_id = uuid.UUID(str(payload["cluster_id"]))
    alert_name = str(payload.get("alert_name") or "unknown")

    async with database.AsyncSessionLocal() as db:
        await heartbeat_job(
            db,
            job.id,
            worker_id=worker_id,
            lease_seconds=default_lease_seconds(),
        )

    await run_incident_investigation(
        incident_id=incident_id,
        cluster_id=cluster_id,
        alert_name=alert_name,
        job_id=job.id,
        alert_labels=payload.get("alert_labels") or {},
        alert_annotations=payload.get("alert_annotations") or {},
        alert_starts_at=payload.get("alert_starts_at"),
        alert_severity=payload.get("alert_severity") or "warning",
        organization_id=(
            str(job.organization_id) if job.organization_id is not None else None
        ),
        admission_owner=worker_id,
    )

    async with database.AsyncSessionLocal() as db:
        await complete_job(db, job.id, worker_id=worker_id, result_payload={"ok": True})


async def worker_loop(worker_id: Optional[str] = None) -> None:
    owner = worker_id or _worker_id()
    logger.info("Durable job worker started as %s", owner)
    while not _STOP.is_set():
        try:
            from sre_agent.concurrency import get_admission_controller

            admission = get_admission_controller()
            available = int(admission.stats()["available"])
            if available < 1:
                try:
                    await asyncio.wait_for(_STOP.wait(), timeout=_poll_interval())
                except asyncio.TimeoutError:
                    pass
                continue

            async with database.AsyncSessionLocal() as db:
                claimed = await claim_jobs(
                    db,
                    worker_id=owner,
                    limit=min(_batch_size(), available),
                    lease_seconds=default_lease_seconds(),
                )
            if not claimed:
                try:
                    await asyncio.wait_for(_STOP.wait(), timeout=_poll_interval())
                except asyncio.TimeoutError:
                    pass
                continue
            for job in claimed:
                if _STOP.is_set():
                    break
                try:
                    await execute_claimed_job(job, worker_id=owner)
                except DurableJobError as exc:
                    logger.warning("Job %s cancelled or lost lease: %s", job.id, exc)
                    async with database.AsyncSessionLocal() as db:
                        try:
                            await fail_job(db, job.id, worker_id=owner, error=str(exc))
                        except DurableJobError:
                            pass
                except Exception as exc:
                    logger.exception("Job %s failed: %s", job.id, exc)
                    async with database.AsyncSessionLocal() as db:
                        try:
                            await fail_job(db, job.id, worker_id=owner, error=str(exc))
                        except DurableJobError:
                            pass
        except Exception as exc:
            logger.exception("Durable job worker loop error: %s", exc)
            await asyncio.sleep(_poll_interval())
    logger.info("Durable job worker stopped (%s)", owner)


def start_job_worker() -> asyncio.Task:
    global _WORKER_TASK
    _STOP.clear()
    if _WORKER_TASK and not _WORKER_TASK.done():
        return _WORKER_TASK
    _WORKER_TASK = asyncio.create_task(worker_loop(), name="durable-job-worker")
    return _WORKER_TASK


async def stop_job_worker() -> None:
    _STOP.set()
    task = _WORKER_TASK
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()


async def enqueue_and_kick(
    *,
    db,
    cluster_id: uuid.UUID,
    organization_id: Optional[uuid.UUID],
    incident_id: uuid.UUID,
    alert_name: str,
    alert_labels: Optional[dict[str, Any]] = None,
    alert_annotations: Optional[dict[str, Any]] = None,
    alert_starts_at: Optional[str] = None,
    alert_severity: Optional[str] = None,
    triggered_by: str = "durable_queue",
):
    """Persist an investigation job and ensure a worker is running in-process."""
    from sre_agent.durable_jobs import (
        encode_investigation_payload,
        investigation_idempotency_key,
    )
    from sre_agent.job_store import enqueue_investigation

    payload = encode_investigation_payload(
        incident_id=incident_id,
        cluster_id=cluster_id,
        alert_name=alert_name,
        alert_labels=alert_labels,
        alert_annotations=alert_annotations,
        alert_starts_at=alert_starts_at,
        alert_severity=alert_severity,
        triggered_by=triggered_by,
    )
    job = await enqueue_investigation(
        db,
        cluster_id=cluster_id,
        organization_id=organization_id,
        incident_id=incident_id,
        payload=payload,
        idempotency_key=investigation_idempotency_key(incident_id),
    )
    if os.getenv("JOB_WORKER_ENABLED", "true").lower() in {"1", "true", "yes"}:
        start_job_worker()
    return job
