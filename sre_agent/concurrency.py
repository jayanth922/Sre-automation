#!/usr/bin/env python3
"""
Investigation concurrency + sandboxing with fail-closed lease admission (R08).

Bounds how many investigations run concurrently using timed leases. On timeout
the caller must remain queued or fail explicitly — never continue without a
reserved slot. Leases expire so crashed owners cannot permanently consume capacity.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class AtCapacityError(RuntimeError):
    """Raised when no investigation slot is available within the wait budget."""


def default_max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_INVESTIGATIONS", "5")))
    except ValueError:
        return 5


def default_max_per_organization() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_INVESTIGATIONS_PER_ORG", "0")))
    except ValueError:
        return 0


def default_admission_lease_seconds() -> int:
    try:
        return max(15, int(os.getenv("INVESTIGATION_ADMISSION_LEASE_SECONDS", "120")))
    except ValueError:
        return 120


def default_admission_wait_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("INVESTIGATION_ADMISSION_WAIT_SECONDS", "120")))
    except ValueError:
        return 120.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AdmissionLease:
    incident_id: str
    organization_id: str
    owner: str
    acquired_at: datetime
    expires_at: datetime

    def to_dict(self) -> Dict[str, str]:
        return {
            "incident_id": self.incident_id,
            "organization_id": self.organization_id,
            "owner": self.owner,
            "acquired_at": self.acquired_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class InvestigationLimiter:
    """Bounded, idempotent in-process slot allocator keyed by incident_id."""

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        self.max = (
            max_concurrent if max_concurrent is not None else default_max_concurrent()
        )
        self._active: Set[str] = set()

    def try_acquire(self, incident_id: str) -> bool:
        if incident_id in self._active:
            return True
        if len(self._active) >= self.max:
            return False
        self._active.add(incident_id)
        return True

    def release(self, incident_id: str) -> None:
        self._active.discard(incident_id)

    @property
    def active(self) -> int:
        return len(self._active)

    def stats(self) -> Dict[str, int]:
        return {
            "active": len(self._active),
            "capacity": self.max,
            "available": max(0, self.max - len(self._active)),
        }

    @contextmanager
    def slot(self, incident_id: str):
        if not self.try_acquire(incident_id):
            raise AtCapacityError(
                f"at capacity ({self.max} concurrent investigations); "
                f"shed or queue incident {incident_id}"
            )
        try:
            yield
        finally:
            self.release(incident_id)


class LeaseAdmissionController:
    """Fail-closed lease-backed admission with optional per-org quotas."""

    def __init__(
        self,
        *,
        max_concurrent: Optional[int] = None,
        max_per_organization: Optional[int] = None,
        lease_seconds: Optional[int] = None,
    ) -> None:
        self.max = (
            max_concurrent if max_concurrent is not None else default_max_concurrent()
        )
        per_org = (
            max_per_organization
            if max_per_organization is not None
            else default_max_per_organization()
        )
        self.max_per_organization = per_org if per_org > 0 else None
        self.lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else default_admission_lease_seconds()
        )
        self._leases: Dict[str, AdmissionLease] = {}

    def reclaim_expired(self, *, now: Optional[datetime] = None) -> int:
        clock = now or _utcnow()
        expired = [
            incident_id
            for incident_id, lease in self._leases.items()
            if lease.expires_at <= clock
        ]
        for incident_id in expired:
            del self._leases[incident_id]
        return len(expired)

    def try_acquire(
        self,
        incident_id: str,
        *,
        organization_id: str = "default",
        owner: str = "worker",
        now: Optional[datetime] = None,
    ) -> Optional[AdmissionLease]:
        clock = now or _utcnow()
        self.reclaim_expired(now=clock)
        existing = self._leases.get(incident_id)
        if existing is not None:
            existing.expires_at = clock + timedelta(seconds=self.lease_seconds)
            existing.owner = owner
            return existing
        if len(self._leases) >= self.max:
            return None
        if self.max_per_organization is not None:
            org_count = sum(
                1
                for lease in self._leases.values()
                if lease.organization_id == organization_id
            )
            if org_count >= self.max_per_organization:
                return None
        lease = AdmissionLease(
            incident_id=incident_id,
            organization_id=organization_id,
            owner=owner,
            acquired_at=clock,
            expires_at=clock + timedelta(seconds=self.lease_seconds),
        )
        self._leases[incident_id] = lease
        return lease

    def heartbeat(
        self,
        incident_id: str,
        *,
        owner: str,
        now: Optional[datetime] = None,
    ) -> AdmissionLease:
        clock = now or _utcnow()
        self.reclaim_expired(now=clock)
        lease = self._leases.get(incident_id)
        if lease is None or lease.owner != owner:
            raise AtCapacityError(
                f"admission lease for {incident_id} is missing or owned by another worker"
            )
        lease.expires_at = clock + timedelta(seconds=self.lease_seconds)
        return lease

    def release(self, incident_id: str, *, owner: Optional[str] = None) -> None:
        lease = self._leases.get(incident_id)
        if lease is None:
            return
        if owner is not None and lease.owner != owner:
            raise AtCapacityError(
                f"cannot release admission lease for {incident_id}: owner mismatch"
            )
        del self._leases[incident_id]

    def owner(self, incident_id: str) -> Optional[str]:
        self.reclaim_expired()
        lease = self._leases.get(incident_id)
        return None if lease is None else lease.owner

    def stats(
        self, *, organization_id: Optional[str] = None
    ) -> Dict[str, int | str | None]:
        self.reclaim_expired()
        active = len(self._leases)
        org_active = None
        if organization_id is not None:
            org_active = sum(
                1
                for lease in self._leases.values()
                if lease.organization_id == organization_id
            )
        return {
            "active": active,
            "capacity": self.max,
            "available": max(0, self.max - active),
            "per_organization_capacity": self.max_per_organization,
            "organization_active": org_active,
            "lease_seconds": self.lease_seconds,
        }

    def acquire_or_fail(
        self,
        incident_id: str,
        *,
        organization_id: str = "default",
        owner: str = "worker",
        wait_seconds: Optional[float] = None,
        poll_seconds: float = 0.05,
    ) -> AdmissionLease:
        """Wait up to wait_seconds, then fail closed — never run without a lease."""
        budget = (
            default_admission_wait_seconds()
            if wait_seconds is None
            else max(0.0, float(wait_seconds))
        )
        deadline = time.monotonic() + budget
        waited = 0.0
        while True:
            lease = self.try_acquire(
                incident_id,
                organization_id=organization_id,
                owner=owner,
            )
            if lease is not None:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stats = self.stats(organization_id=organization_id)
                raise AtCapacityError(
                    "admission timeout without a reserved slot; "
                    f"capacity={stats['capacity']} active={stats['active']} "
                    f"org_active={stats['organization_active']} "
                    f"incident={incident_id}"
                )
            sleep_for = min(poll_seconds, remaining)
            time.sleep(sleep_for)
            waited += sleep_for

    async def async_acquire_or_fail(
        self,
        incident_id: str,
        *,
        organization_id: str = "default",
        owner: str = "worker",
        wait_seconds: Optional[float] = None,
        poll_seconds: float = 0.25,
    ) -> AdmissionLease:
        import asyncio

        budget = (
            default_admission_wait_seconds()
            if wait_seconds is None
            else max(0.0, float(wait_seconds))
        )
        deadline = time.monotonic() + budget
        while True:
            lease = self.try_acquire(
                incident_id,
                organization_id=organization_id,
                owner=owner,
            )
            if lease is not None:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stats = self.stats(organization_id=organization_id)
                raise AtCapacityError(
                    "admission timeout without a reserved slot; "
                    f"capacity={stats['capacity']} active={stats['active']} "
                    f"org_active={stats['organization_active']} "
                    f"incident={incident_id}"
                )
            await asyncio.sleep(min(poll_seconds, remaining))


_GLOBAL_LIMITER: Optional[InvestigationLimiter] = None
_GLOBAL_ADMISSION: Optional[LeaseAdmissionController] = None


def get_limiter() -> InvestigationLimiter:
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is None:
        _GLOBAL_LIMITER = InvestigationLimiter()
    return _GLOBAL_LIMITER


def get_admission_controller() -> LeaseAdmissionController:
    global _GLOBAL_ADMISSION
    if _GLOBAL_ADMISSION is None:
        _GLOBAL_ADMISSION = LeaseAdmissionController()
    return _GLOBAL_ADMISSION


@dataclass
class Sandbox:
    incident_id: str
    workspace: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def create_sandbox(incident_id: str, base_dir: Optional[str] = None) -> Sandbox:
    """Create an isolated per-investigation workspace directory."""
    base = base_dir or os.getenv("SANDBOX_BASE_DIR") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix=f"inv-{incident_id}-", dir=base)
    logger.info(f"🧰 Sandbox created for {incident_id}: {workspace}")
    return Sandbox(incident_id=incident_id, workspace=workspace)
