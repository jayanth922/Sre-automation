#!/usr/bin/env python3
"""R02 durable investigation jobs with lease ownership, retries, and cancellation."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

SCHEMA_VERSION = 1
_ACTIVE = {"pending", "running"}
_TERMINAL = {"completed", "cancelled", "dead_letter"}


class DurableJobError(ValueError):
    """Job lease or queue contract was violated."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DurableJobError(f"{field_name} must be a non-empty string")
    return value.strip()


def investigation_idempotency_key(incident_id: uuid.UUID | str) -> str:
    return f"investigation:{incident_id}"


@dataclass
class DurableJob:
    id: uuid.UUID
    cluster_id: uuid.UUID
    organization_id: Optional[uuid.UUID]
    incident_id: Optional[uuid.UUID]
    job_type: str
    status: str
    payload: dict[str, Any]
    idempotency_key: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=_utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "id",
            "cluster_id",
            "organization_id",
            "incident_id",
        ):
            if value[key] is not None:
                value[key] = str(value[key])
        for key in (
            "lease_expires_at",
            "heartbeat_at",
            "cancel_requested_at",
            "created_at",
            "started_at",
            "completed_at",
        ):
            if value[key] is not None:
                value[key] = value[key].isoformat()
        return value


class InMemoryDurableJobStore:
    """Process-local store used by unit tests and optional local mode."""

    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, DurableJob] = {}

    def enqueue(
        self,
        *,
        cluster_id: uuid.UUID,
        organization_id: Optional[uuid.UUID],
        incident_id: Optional[uuid.UUID],
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> DurableJob:
        if max_attempts < 1:
            raise DurableJobError("max_attempts must be at least 1")
        if idempotency_key:
            for existing in self._jobs.values():
                if (
                    existing.cluster_id == cluster_id
                    and existing.idempotency_key == idempotency_key
                    and existing.status in _ACTIVE
                ):
                    return existing
        job = DurableJob(
            id=uuid.uuid4(),
            cluster_id=cluster_id,
            organization_id=organization_id,
            incident_id=incident_id,
            job_type=_string(job_type, "job_type"),
            status="pending",
            payload=dict(payload),
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        self._jobs[job.id] = job
        return job

    def reclaim_expired(self, *, now: Optional[datetime] = None) -> list[DurableJob]:
        clock = now or _utcnow()
        reclaimed: list[DurableJob] = []
        for job in self._jobs.values():
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at <= clock
            ):
                if job.attempt_count >= job.max_attempts:
                    job.status = "dead_letter"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.completed_at = clock
                    job.last_error = (
                        job.last_error or "lease expired after max attempts"
                    )
                else:
                    job.status = "pending"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                reclaimed.append(job)
        return reclaimed

    def claim(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 60,
        now: Optional[datetime] = None,
    ) -> list[DurableJob]:
        if limit < 1:
            raise DurableJobError("limit must be at least 1")
        if lease_seconds < 1:
            raise DurableJobError("lease_seconds must be at least 1")
        owner = _string(worker_id, "worker_id")
        clock = now or _utcnow()
        self.reclaim_expired(now=clock)
        pending = [
            job
            for job in self._jobs.values()
            if job.status == "pending" and job.cancel_requested_at is None
        ]
        # Per-tenant fairness: round-robin by organization, then oldest first.
        pending.sort(
            key=lambda job: (
                str(job.organization_id or job.cluster_id),
                job.created_at,
            )
        )
        claimed: list[DurableJob] = []
        seen_orgs: set[str] = set()
        fair: list[DurableJob] = []
        remainder: list[DurableJob] = []
        for job in pending:
            org = str(job.organization_id or job.cluster_id)
            if org in seen_orgs:
                remainder.append(job)
            else:
                fair.append(job)
                seen_orgs.add(org)
        ordered = fair + remainder
        for job in ordered[:limit]:
            if job.cancel_requested_at is not None:
                job.status = "cancelled"
                job.completed_at = clock
                continue
            job.status = "running"
            job.attempt_count += 1
            job.lease_owner = owner
            job.lease_expires_at = clock + timedelta(seconds=lease_seconds)
            job.heartbeat_at = clock
            job.started_at = job.started_at or clock
            claimed.append(job)
        return claimed

    def heartbeat(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: Optional[datetime] = None,
    ) -> DurableJob:
        job = self._require(job_id)
        owner = _string(worker_id, "worker_id")
        clock = now or _utcnow()
        if job.status != "running" or job.lease_owner != owner:
            raise DurableJobError("cannot heartbeat a job without ownership")
        if job.lease_expires_at is not None and job.lease_expires_at < clock:
            raise DurableJobError("lease expired")
        if job.cancel_requested_at is not None:
            raise DurableJobError("job cancellation requested")
        job.heartbeat_at = clock
        job.lease_expires_at = clock + timedelta(seconds=lease_seconds)
        return job

    def request_cancel(
        self, job_id: uuid.UUID, *, now: Optional[datetime] = None
    ) -> DurableJob:
        job = self._require(job_id)
        clock = now or _utcnow()
        if job.status in _TERMINAL:
            return job
        job.cancel_requested_at = clock
        if job.status == "pending":
            job.status = "cancelled"
            job.completed_at = clock
        return job

    def complete(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        result: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> DurableJob:
        job = self._require_owner(job_id, worker_id)
        clock = now or _utcnow()
        job.status = "completed"
        job.completed_at = clock
        job.lease_owner = None
        job.lease_expires_at = None
        if result is not None:
            job.payload = {**job.payload, "result": result}
        return job

    def fail(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error: str,
        now: Optional[datetime] = None,
    ) -> DurableJob:
        job = self._require_owner(job_id, worker_id)
        clock = now or _utcnow()
        job.last_error = _string(error, "error")
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        if job.cancel_requested_at is not None:
            job.status = "cancelled"
            job.completed_at = clock
        elif job.attempt_count >= job.max_attempts:
            job.status = "dead_letter"
            job.completed_at = clock
        else:
            job.status = "pending"
        return job

    def get(self, job_id: uuid.UUID) -> Optional[DurableJob]:
        return self._jobs.get(job_id)

    def all(self) -> list[DurableJob]:
        return list(self._jobs.values())

    def _require(self, job_id: uuid.UUID) -> DurableJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise DurableJobError(f"job not found: {job_id}")
        return job

    def _require_owner(self, job_id: uuid.UUID, worker_id: str) -> DurableJob:
        job = self._require(job_id)
        if job.status != "running" or job.lease_owner != _string(
            worker_id, "worker_id"
        ):
            raise DurableJobError("worker does not own this job lease")
        return job


_GLOBAL_STORE: Optional[InMemoryDurableJobStore] = None


def get_memory_job_store() -> InMemoryDurableJobStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = InMemoryDurableJobStore()
    return _GLOBAL_STORE


def default_lease_seconds() -> int:
    try:
        return max(15, int(os.getenv("JOB_LEASE_SECONDS", "60")))
    except ValueError:
        return 60


def default_max_attempts() -> int:
    try:
        return max(1, int(os.getenv("JOB_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


def encode_investigation_payload(
    *,
    incident_id: uuid.UUID | str,
    cluster_id: uuid.UUID | str,
    alert_name: str,
    alert_labels: Optional[dict[str, Any]] = None,
    alert_annotations: Optional[dict[str, Any]] = None,
    alert_starts_at: Optional[str] = None,
    alert_severity: Optional[str] = None,
    triggered_by: str = "durable_queue",
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "handler": "run_graph_background_saas",
        "incident_id": str(incident_id),
        "cluster_id": str(cluster_id),
        "alert_name": alert_name,
        "alert_labels": alert_labels or {},
        "alert_annotations": alert_annotations or {},
        "alert_starts_at": alert_starts_at,
        "alert_severity": alert_severity or "warning",
        "triggered_by": triggered_by,
    }


def payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
