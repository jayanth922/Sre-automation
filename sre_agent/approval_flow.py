"""Durable approval primitives shared by the graph and approval API."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


class ApprovalValidationError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def approval_ttl() -> timedelta:
    raw = os.getenv("APPROVAL_TTL_MINUTES", "30")
    try:
        minutes = max(1, int(raw))
    except (TypeError, ValueError):
        minutes = 30
    return timedelta(minutes=minutes)


def canonical_action_json(report_payload: Dict[str, Any]) -> str:
    """Serialize the exact proposed report deterministically for authorization."""

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                # Dry-run audit records include their creation timestamp in this
                # derived hash. It is evidence, not part of the proposed action.
                # Evidence observation timestamps similarly must not destabilize
                # approval hashes across identical proposals.
                if key not in {"audit_hash", "observed_at"}
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return json.dumps(
        stable(report_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def compute_action_hash(report_payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_action_json(report_payload).encode("utf-8")
    ).hexdigest()


def is_expired(expires_at: datetime, now: Optional[datetime] = None) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or utc_now())


def validate_pending_approval(
    *,
    status: Any,
    stored_action_hash: str,
    submitted_action_hash: str,
    expires_at: datetime,
    now: Optional[datetime] = None,
) -> None:
    """Reject replay, expiry, or mutation before any approval state transition."""
    if status != "pending":
        raise ApprovalValidationError("not_pending")
    if is_expired(expires_at, now):
        raise ApprovalValidationError("expired")
    if not secrets.compare_digest(stored_action_hash, submitted_action_hash):
        raise ApprovalValidationError("hash_mismatch")


@dataclass(frozen=True)
class PendingApproval:
    id: str
    incident_id: str
    thread_id: str
    action_hash: str
    expires_at: datetime

    def interrupt_payload(self, report_payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "approval_required",
            "approval_request_id": self.id,
            "incident_id": self.incident_id,
            "thread_id": self.thread_id,
            "report": report_payload,
            "action_hash": self.action_hash,
            "expires_at": self.expires_at.isoformat(),
        }


def current_approval_interrupt(snapshot: Any) -> Optional[Dict[str, Any]]:
    """Return the active approval interrupt from a LangGraph StateSnapshot."""
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = (
            task.get("interrupts", ())
            if isinstance(task, dict)
            else getattr(task, "interrupts", ())
        ) or ()
        for item in interrupts:
            value = (
                item.get("value")
                if isinstance(item, dict)
                else getattr(item, "value", None)
            )
            if isinstance(value, dict) and value.get("type") == "approval_required":
                return value
    return None


async def create_or_reuse_pending_approval(
    *,
    incident_id: str,
    thread_id: str,
    organization_id: str,
    cluster_id: str,
    action_hash: str,
) -> PendingApproval:
    """Persist the authorization before its graph interrupt is checkpointed.

    The lookup makes node retries idempotent if the process dies after the
    database commit but before LangGraph writes the next checkpoint.
    """
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError

    from backend import database, models

    incident_uuid = uuid.UUID(str(incident_id))
    organization_uuid = uuid.UUID(str(organization_id))
    cluster_uuid = uuid.UUID(str(cluster_id))
    now = utc_now()

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.ApprovalRequest)
            .where(
                models.ApprovalRequest.incident_id == incident_uuid,
                models.ApprovalRequest.thread_id == thread_id,
                models.ApprovalRequest.action_hash == action_hash,
                models.ApprovalRequest.organization_id == organization_uuid,
                models.ApprovalRequest.cluster_id == cluster_uuid,
                models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            )
            .order_by(models.ApprovalRequest.created_at.desc())
            .limit(1)
        )
        request = result.scalar_one_or_none()

        if request is not None and is_expired(request.expires_at, now):
            request.status = models.ApprovalStatus.EXPIRED
            await db.flush()
            request = None

        created = request is None
        if created:
            request = models.ApprovalRequest(
                incident_id=incident_uuid,
                thread_id=thread_id,
                action_hash=action_hash,
                organization_id=organization_uuid,
                cluster_id=cluster_uuid,
                status=models.ApprovalStatus.PENDING,
                expires_at=now + approval_ttl(),
            )
            db.add(request)

        await db.execute(
            update(models.Incident)
            .where(
                models.Incident.id == incident_uuid,
                models.Incident.cluster_id == cluster_uuid,
            )
            .values(status=models.IncidentStatus.AWAITING_APPROVAL)
        )

        try:
            await db.commit()
        except IntegrityError:
            if not created:
                raise
            await db.rollback()
            result = await db.execute(
                select(models.ApprovalRequest).where(
                    models.ApprovalRequest.incident_id == incident_uuid,
                    models.ApprovalRequest.thread_id == thread_id,
                    models.ApprovalRequest.action_hash == action_hash,
                    models.ApprovalRequest.organization_id == organization_uuid,
                    models.ApprovalRequest.cluster_id == cluster_uuid,
                    models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
                )
            )
            request = result.scalar_one_or_none()
            if request is None:
                raise
        await db.refresh(request)
        return PendingApproval(
            id=str(request.id),
            incident_id=str(request.incident_id),
            thread_id=request.thread_id,
            action_hash=request.action_hash,
            expires_at=request.expires_at,
        )
