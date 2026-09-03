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


# ── Phase 5's two Temporal remediation gates (start_fix, raise_pr) ──────────
#
# Distinct from PendingApproval/ApprovalRequest above: keyed off a Temporal
# `workflow_id` + `gate` rather than a LangGraph `thread_id` + `action_hash`,
# since there is no "exact report" to hash — deciding a gate just signals a
# running IncidentRemediationWorkflow (sre_agent/incident_remediation_workflow.py).


@dataclass(frozen=True)
class PendingGateApproval:
    id: str
    incident_id: str
    organization_id: str
    cluster_id: str
    workflow_id: str
    gate: str
    expires_at: datetime


async def create_or_reuse_pending_gate_approval(
    *,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
    workflow_id: str,
    gate: str,
    ttl_seconds: int,
) -> PendingGateApproval:
    """Persist one gate's PENDING state as a durable row so the dashboard/API
    has something to list and act on. Idempotent lookup-or-create mirrors
    create_or_reuse_pending_approval's retry safety, keyed off
    (workflow_id, gate) instead of (incident_id, thread_id, action_hash)
    since a Temporal workflow_id is already a unique run identifier.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from backend import database, models

    incident_uuid = uuid.UUID(str(incident_id))
    organization_uuid = uuid.UUID(str(organization_id))
    cluster_uuid = uuid.UUID(str(cluster_id))
    now = utc_now()
    ttl = timedelta(seconds=max(1, int(ttl_seconds)))

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.RemediationGateApproval)
            .where(
                models.RemediationGateApproval.workflow_id == workflow_id,
                models.RemediationGateApproval.gate == gate,
                models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
            )
            .order_by(models.RemediationGateApproval.created_at.desc())
            .limit(1)
        )
        request = result.scalar_one_or_none()

        if request is not None and is_expired(request.expires_at, now):
            request.status = models.ApprovalStatus.EXPIRED
            await db.flush()
            request = None

        created = request is None
        if created:
            request = models.RemediationGateApproval(
                incident_id=incident_uuid,
                organization_id=organization_uuid,
                cluster_id=cluster_uuid,
                workflow_id=workflow_id,
                gate=gate,
                status=models.ApprovalStatus.PENDING,
                expires_at=now + ttl,
            )
            db.add(request)

        try:
            await db.commit()
        except IntegrityError:
            if not created:
                raise
            await db.rollback()
            result = await db.execute(
                select(models.RemediationGateApproval).where(
                    models.RemediationGateApproval.workflow_id == workflow_id,
                    models.RemediationGateApproval.gate == gate,
                    models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
                )
            )
            request = result.scalar_one_or_none()
            if request is None:
                raise
        await db.refresh(request)
        return PendingGateApproval(
            id=str(request.id),
            incident_id=str(request.incident_id),
            organization_id=str(request.organization_id),
            cluster_id=str(request.cluster_id),
            workflow_id=request.workflow_id,
            gate=request.gate,
            expires_at=request.expires_at,
        )


async def expire_gate_approval(*, workflow_id: str, gate: str) -> None:
    """Reflect a workflow-driven wait_condition timeout back into the DB row
    so the dashboard stops showing a stale PENDING gate. Best-effort no-op if
    the row was already decided by a racing API call.
    """
    from sqlalchemy import update

    from backend import database, models

    async with database.AsyncSessionLocal() as db:
        await db.execute(
            update(models.RemediationGateApproval)
            .where(
                models.RemediationGateApproval.workflow_id == workflow_id,
                models.RemediationGateApproval.gate == gate,
                models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
            )
            .values(status=models.ApprovalStatus.EXPIRED, decided_at=utc_now())
        )
        await db.commit()


_GATE_SIGNAL_NAME = {"start_fix": "decide_start_fix", "raise_pr": "decide_raise_pr"}


async def find_latest_pending_gate(*, incident_id: str, gate: str) -> Optional[str]:
    """Return the id of the newest PENDING RemediationGateApproval row for
    this incident+gate, or None. The dashboard already knows a row's id from
    its GET /remediation-gates listing; inbound transports that only know
    (incident, gate) — e.g. a Slack "approve start-fix" reply — resolve the
    id through here first.
    """
    from sqlalchemy import select

    from backend import database, models

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.RemediationGateApproval.id)
            .where(
                models.RemediationGateApproval.incident_id == uuid.UUID(str(incident_id)),
                models.RemediationGateApproval.gate == gate,
                models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
            )
            .order_by(models.RemediationGateApproval.created_at.desc())
            .limit(1)
        )
        row_id = result.scalar_one_or_none()
        return str(row_id) if row_id is not None else None


async def decide_and_signal_gate(
    *,
    gate_approval_id: str,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
    approved: bool,
    approver_user_id: str,
    approver_label: str,
):
    """decide_gate_approval, then signal the waiting Temporal workflow.

    Shared by every transport that can decide a gate (dashboard API in
    sre_agent/api/v1/remediation_gates.py, Slack in war_room.py) so they
    can't drift on the gate->signal-name mapping or the decide/signal order.
    Raises ApprovalValidationError same as decide_gate_approval. Returns
    (row_or_None, delivered) — delivered is False when the row was decided
    but the running workflow (if any) could not be signaled.
    """
    row = await decide_gate_approval(
        gate_approval_id=gate_approval_id,
        incident_id=incident_id,
        organization_id=organization_id,
        cluster_id=cluster_id,
        approved=approved,
        approver_user_id=approver_user_id,
    )
    if row is None:
        return None, False

    signal_name = _GATE_SIGNAL_NAME.get(row.gate)
    if signal_name is None:
        return row, False

    from .temporal_client import signal_workflow

    delivered = await signal_workflow(row.workflow_id, signal_name, args=[approved, approver_label])
    return row, delivered


async def decide_gate_approval(
    *,
    gate_approval_id: str,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
    approved: bool,
    approver_user_id: str,
) -> Optional[PendingGateApproval]:
    """Ownership-scoped CAS decision on one gate row, mirroring
    mission_control.approve_incident_action's ApprovalRequest CAS block.

    Raises ApprovalValidationError("not_pending" | "expired") for the caller
    to translate to HTTP status codes. Returns None if no row matches the
    ownership scope at all (caller treats that as 404).
    """
    from sqlalchemy import select, update

    from backend import database, models

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.RemediationGateApproval).where(
                models.RemediationGateApproval.id == uuid.UUID(str(gate_approval_id)),
                models.RemediationGateApproval.incident_id == uuid.UUID(str(incident_id)),
                models.RemediationGateApproval.organization_id == uuid.UUID(str(organization_id)),
                models.RemediationGateApproval.cluster_id == uuid.UUID(str(cluster_id)),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None

        now = utc_now()
        if row.status != models.ApprovalStatus.PENDING:
            raise ApprovalValidationError("not_pending")
        if is_expired(row.expires_at, now):
            await db.execute(
                update(models.RemediationGateApproval)
                .where(
                    models.RemediationGateApproval.id == row.id,
                    models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
                )
                .values(status=models.ApprovalStatus.EXPIRED, decided_at=now)
            )
            await db.commit()
            raise ApprovalValidationError("expired")

        new_status = models.ApprovalStatus.APPROVED if approved else models.ApprovalStatus.REJECTED
        cas = await db.execute(
            update(models.RemediationGateApproval)
            .where(
                models.RemediationGateApproval.id == row.id,
                models.RemediationGateApproval.status == models.ApprovalStatus.PENDING,
            )
            .values(
                status=new_status,
                approver_user_id=uuid.UUID(str(approver_user_id)),
                decided_at=now,
            )
        )
        if cas.rowcount != 1:
            await db.rollback()
            raise ApprovalValidationError("not_pending")
        await db.commit()
        await db.refresh(row)
        return PendingGateApproval(
            id=str(row.id),
            incident_id=str(row.incident_id),
            organization_id=str(row.organization_id),
            cluster_id=str(row.cluster_id),
            workflow_id=row.workflow_id,
            gate=row.gate,
            expires_at=row.expires_at,
        )
