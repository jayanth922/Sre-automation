#!/usr/bin/env python3
"""SQLAlchemy persistence adapter for R02 durable jobs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import models
from sre_agent.durable_jobs import (
    DurableJob,
    DurableJobError,
    default_max_attempts,
    payload_json,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_payload(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _to_record(job: models.Job) -> DurableJob:
    return DurableJob(
        id=job.id,
        cluster_id=job.cluster_id,
        organization_id=job.organization_id,
        incident_id=job.incident_id,
        job_type=str(
            job.job_type.value if hasattr(job.job_type, "value") else job.job_type
        ),
        status=str(job.status.value if hasattr(job.status, "value") else job.status),
        payload=_parse_payload(job.payload),
        idempotency_key=job.idempotency_key,
        attempt_count=int(job.attempt_count or 0),
        max_attempts=int(job.max_attempts or 3),
        lease_owner=job.lease_owner,
        lease_expires_at=job.lease_expires_at,
        heartbeat_at=job.heartbeat_at,
        cancel_requested_at=job.cancel_requested_at,
        last_error=job.last_error,
        created_at=job.created_at or _utcnow(),
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


async def enqueue_investigation(
    db: AsyncSession,
    *,
    cluster_id: uuid.UUID,
    organization_id: Optional[uuid.UUID],
    incident_id: uuid.UUID,
    payload: dict[str, Any],
    idempotency_key: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> models.Job:
    key = idempotency_key or f"investigation:{incident_id}"
    attempts = max_attempts if max_attempts is not None else default_max_attempts()
    if attempts < 1:
        raise DurableJobError("max_attempts must be at least 1")

    existing = await db.execute(
        select(models.Job).where(
            models.Job.cluster_id == cluster_id,
            models.Job.idempotency_key == key,
            models.Job.status.in_([models.JobStatus.PENDING, models.JobStatus.RUNNING]),
        )
    )
    found = existing.scalars().first()
    if found is not None:
        return found

    job = models.Job(
        cluster_id=cluster_id,
        organization_id=organization_id,
        incident_id=incident_id,
        job_type=models.JobType.INVESTIGATION,
        status=models.JobStatus.PENDING,
        payload=payload_json(payload),
        idempotency_key=key,
        attempt_count=0,
        max_attempts=attempts,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def reclaim_expired_leases(
    db: AsyncSession, *, now: Optional[datetime] = None
) -> int:
    clock = now or _utcnow()
    result = await db.execute(
        select(models.Job).where(
            models.Job.status == models.JobStatus.RUNNING,
            models.Job.lease_expires_at.is_not(None),
            models.Job.lease_expires_at <= clock,
        )
    )
    count = 0
    for job in result.scalars().all():
        count += 1
        if int(job.attempt_count or 0) >= int(job.max_attempts or 3):
            job.status = models.JobStatus.DEAD_LETTER
            job.completed_at = clock
            job.last_error = job.last_error or "lease expired after max attempts"
        else:
            job.status = models.JobStatus.PENDING
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
    if count:
        await db.commit()
    return count


async def claim_jobs(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 60,
    now: Optional[datetime] = None,
) -> list[DurableJob]:
    if limit < 1 or lease_seconds < 1:
        raise DurableJobError("limit and lease_seconds must be positive")
    clock = now or _utcnow()
    await reclaim_expired_leases(db, now=clock)

    # Cancel any pending jobs that were already asked to stop.
    pending_cancel = await db.execute(
        select(models.Job).where(
            models.Job.status == models.JobStatus.PENDING,
            models.Job.cancel_requested_at.is_not(None),
        )
    )
    for job in pending_cancel.scalars().all():
        job.status = models.JobStatus.CANCELLED
        job.completed_at = clock
    await db.commit()

    result = await db.execute(
        select(models.Job)
        .where(
            models.Job.status == models.JobStatus.PENDING,
            models.Job.cancel_requested_at.is_(None),
        )
        .order_by(models.Job.created_at.asc())
        .limit(max(limit * 8, limit))
        .with_for_update(skip_locked=True)
    )
    candidates = list(result.scalars().all())
    claimed_models: list[models.Job] = []
    seen_orgs: set[str] = set()
    fair: list[models.Job] = []
    remainder: list[models.Job] = []
    for job in candidates:
        org = str(job.organization_id or job.cluster_id)
        if org in seen_orgs:
            remainder.append(job)
        else:
            fair.append(job)
            seen_orgs.add(org)
    for job in (fair + remainder)[:limit]:
        job.status = models.JobStatus.RUNNING
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.lease_owner = worker_id
        job.lease_expires_at = clock + timedelta(seconds=lease_seconds)
        job.heartbeat_at = clock
        job.started_at = job.started_at or clock
        claimed_models.append(job)
    if claimed_models:
        await db.commit()
        for job in claimed_models:
            await db.refresh(job)
    return [_to_record(job) for job in claimed_models]


async def heartbeat_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now: Optional[datetime] = None,
) -> DurableJob:
    clock = now or _utcnow()
    result = await db.execute(select(models.Job).where(models.Job.id == job_id))
    job = result.scalars().first()
    if job is None:
        raise DurableJobError(f"job not found: {job_id}")
    if job.status != models.JobStatus.RUNNING or job.lease_owner != worker_id:
        raise DurableJobError("cannot heartbeat a job without ownership")
    if job.lease_expires_at is not None and job.lease_expires_at < clock:
        raise DurableJobError("lease expired")
    if job.cancel_requested_at is not None:
        raise DurableJobError("job cancellation requested")
    job.heartbeat_at = clock
    job.lease_expires_at = clock + timedelta(seconds=lease_seconds)
    await db.commit()
    await db.refresh(job)
    return _to_record(job)


async def request_job_cancel(
    db: AsyncSession, job_id: uuid.UUID, *, now: Optional[datetime] = None
) -> DurableJob:
    clock = now or _utcnow()
    result = await db.execute(select(models.Job).where(models.Job.id == job_id))
    job = result.scalars().first()
    if job is None:
        raise DurableJobError(f"job not found: {job_id}")
    if job.status in {
        models.JobStatus.COMPLETED,
        models.JobStatus.CANCELLED,
        models.JobStatus.DEAD_LETTER,
    }:
        return _to_record(job)
    job.cancel_requested_at = clock
    if job.status == models.JobStatus.PENDING:
        job.status = models.JobStatus.CANCELLED
        job.completed_at = clock
    await db.commit()
    await db.refresh(job)
    return _to_record(job)


async def complete_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    result_payload: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> DurableJob:
    clock = now or _utcnow()
    result = await db.execute(select(models.Job).where(models.Job.id == job_id))
    job = result.scalars().first()
    if (
        job is None
        or job.status != models.JobStatus.RUNNING
        or job.lease_owner != worker_id
    ):
        raise DurableJobError("worker does not own this job lease")
    job.status = models.JobStatus.COMPLETED
    job.completed_at = clock
    job.lease_owner = None
    job.lease_expires_at = None
    if result_payload is not None:
        job.result = payload_json(result_payload)
    await db.commit()
    await db.refresh(job)
    return _to_record(job)


async def fail_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    error: str,
    now: Optional[datetime] = None,
) -> DurableJob:
    clock = now or _utcnow()
    result = await db.execute(select(models.Job).where(models.Job.id == job_id))
    job = result.scalars().first()
    if (
        job is None
        or job.status != models.JobStatus.RUNNING
        or job.lease_owner != worker_id
    ):
        raise DurableJobError("worker does not own this job lease")
    job.last_error = error
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    if job.cancel_requested_at is not None:
        job.status = models.JobStatus.CANCELLED
        job.completed_at = clock
    elif int(job.attempt_count or 0) >= int(job.max_attempts or 3):
        job.status = models.JobStatus.DEAD_LETTER
        job.completed_at = clock
    else:
        job.status = models.JobStatus.PENDING
    await db.commit()
    await db.refresh(job)
    return _to_record(job)
