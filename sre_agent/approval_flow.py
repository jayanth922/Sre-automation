"""Durable approval primitives shared by the graph and approval API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
    # False when this call reused a row that already existed (a node retry, or a
    # concurrent writer that won the unique index). Announcing an approval is
    # only honest once: the caller uses this to stay idempotent.
    created: bool = True

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


_DECISION_MARK = {
    "autonomous": "✅",
    "requires_approval": "⏸️",
    "blocked": "🚫",
}

# The exact in-thread reply `war_room.is_fix_approval_command` accepts. Named
# here so the message that asks for approval and the handler that grants it
# cannot drift apart.
APPROVAL_COMMAND = "approve fix"


def format_approval_request(
    report_payload: Dict[str, Any],
    expires_at: datetime,
    *,
    max_actions: int = 8,
) -> str:
    """Render the pending remediation as the message a human has to act on.

    Slack is the only channel Sentinel has, so an approval nobody is told about
    is an approval that expires silently — the graph interrupt pauses the run
    but says nothing. This text is what makes the gate real: what would run,
    against what, why a human is needed, the exact words that authorize it, and
    when the offer lapses.
    """
    severity = str(report_payload.get("severity") or "UNKNOWN")
    decision = str(report_payload.get("aggregate_decision") or "requires_approval")
    confidence = str(report_payload.get("confidence_status") or "uncalibrated")
    raw = report_payload.get("raw_action_confidence")
    confidence_line = f"Confidence: {confidence}"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        confidence_line += f" (model self-report {float(raw):.2f})"

    reports = [
        rep
        for rep in (report_payload.get("action_reports") or [])
        if isinstance(rep, dict)
    ]
    held = [rep for rep in reports if str(rep.get("decision")) == "requires_approval"]

    plural = "" if len(reports) == 1 else "s"
    lines = [
        f"🔒 Approval required — severity *{severity}*, plan gated `{decision}`.",
        confidence_line,
    ]

    # When the planner itself died, what follows is a placeholder — one
    # `escalate manual_review` the fallback branch hard-codes — and the per-
    # action reason under it comes from the policy gate, which knows nothing
    # about the crash and will confidently attribute the escalation to
    # something else. Say so before the reader gets there, or the message
    # reads as a considered decision to page a human.
    planning_failed = report_payload.get("planning_failed")
    if planning_failed:
        detail = " ".join(str(planning_failed).split())
        if len(detail) > 300:
            detail = detail[:299] + "…"
        lines += [
            "",
            ":warning: *The planner failed — no remediation was actually "
            "proposed.* The single action below is a placeholder, not a "
            "recommendation, and its stated reason is the policy gate's, not "
            "a diagnosis.",
            f"```{detail}```",
        ]

    lines += [
        "",
        f"Proposed plan ({len(reports)} action{plural}):",
    ]
    for rep in reports[:max_actions]:
        action_decision = str(rep.get("decision") or "unknown")
        mark = _DECISION_MARK.get(action_decision, "•")
        target = str(rep.get("target") or "").strip()
        namespace = str(rep.get("namespace") or "").strip()
        where = f" `{target}`" if target else ""
        if namespace:
            where += f" (ns `{namespace}`)"
        reason = " ".join(str(rep.get("reason") or "").split())
        if len(reason) > 110:
            reason = reason[:109] + "…"
        line = (
            f"{mark} *{rep.get('action_type') or 'action'}*{where}"
            f" — {action_decision} ({rep.get('reversibility') or 'unknown'})"
        )
        lines.append(f"{line}: {reason}" if reason else line)
    if len(reports) > max_actions:
        lines.append(f"… and {len(reports) - max_actions} more.")

    lines.append("")
    if held:
        # Not "N held actions against the cluster": `escalate` only pages a
        # human, and an action in no dispatch map (`code_fix`) cannot run at
        # all. Both were being counted as cluster writes in the one message
        # that gates the entire system. Classify by capability instead.
        from sre_agent.executor import describe_approval_effects

        lines.append(describe_approval_effects(reports))
    lines.append(
        f"Reply `{APPROVAL_COMMAND}` in this thread to authorize. "
        "No reply means nothing runs."
    )
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    lines.append(
        "Expires "
        + expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        + "."
    )
    return "\n".join(lines)


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
            # The concurrent writer created it, and announced it.
            created = False
        await db.refresh(request)
        return PendingApproval(
            id=str(request.id),
            incident_id=str(request.incident_id),
            thread_id=request.thread_id,
            action_hash=request.action_hash,
            expires_at=request.expires_at,
            created=created,
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


_GATE_SIGNAL_NAME = {
    "start_fix": "decide_start_fix",
    "raise_pr": "decide_raise_pr",
    "retry_fix": "decide_retry_fix",
    "close_incident": "decide_close_incident",
}


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


async def acknowledge_incident_resolution(
    *,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
) -> Optional[Any]:
    """CAS an incident's status PENDING_ACKNOWLEDGMENT -> RESOLVED once a
    human confirms a verified fix actually worked, and fire the same
    side effects the old auto-resolve path used to fire inline (closing the
    war room, publishing the "resolved" lifecycle event, transitioning any
    linked Jira issue). Shared by the dashboard's resolve action and Slack's
    "acknowledge" command (sre_agent/war_room.py) so neither surface can
    race the other into a double resolution.

    Raises ApprovalValidationError("not_pending") if the incident isn't
    currently awaiting acknowledgment. Returns None if no incident matches
    the ownership scope (caller treats that as 404).
    """
    from sqlalchemy import update

    from backend import database, models

    incident_uuid = uuid.UUID(str(incident_id))
    organization_uuid = uuid.UUID(str(organization_id))
    cluster_uuid = uuid.UUID(str(cluster_id))
    now = utc_now()

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, incident_uuid)
        if incident is None or str(incident.cluster_id) != str(cluster_uuid):
            return None
        cluster = await db.get(models.Cluster, cluster_uuid)
        if cluster is None or str(cluster.org_id) != str(organization_uuid):
            return None

        if incident.status != models.IncidentStatus.PENDING_ACKNOWLEDGMENT:
            raise ApprovalValidationError("not_pending")

        cas = await db.execute(
            update(models.Incident)
            .where(
                models.Incident.id == incident_uuid,
                models.Incident.status == models.IncidentStatus.PENDING_ACKNOWLEDGMENT,
            )
            .values(status=models.IncidentStatus.RESOLVED, resolved_at=now)
        )
        if cas.rowcount != 1:
            await db.rollback()
            raise ApprovalValidationError("not_pending")
        await db.commit()
        await db.refresh(incident)

    await fire_resolution_side_effects(incident, organization_id, cluster_id)
    return incident


async def fire_resolution_side_effects(
    incident: Any, organization_id: str, cluster_id: str
) -> None:
    """Stop the investigation, close the war room, publish, transition Jira.

    Every path that resolves an incident owes the same four side effects —
    an incident closed without them leaves its Slack thread live, the
    dashboards showing it open, and its investigation still running — so they
    live here once rather than in each caller.
    """
    from backend import database, models

    incident_id = str(incident.id)
    # First, because it is the only one that stops work still being done. An
    # investigation does not notice that its incident was resolved underneath
    # it: it keeps querying, planning, and finally asks a human in Slack to
    # approve a cluster write for an alert that has stopped firing.
    cancelled: list = []
    try:
        from .job_store import cancel_incident_investigations

        async with database.AsyncSessionLocal() as db:
            cancelled = await cancel_incident_investigations(db, incident.id)
        if cancelled:
            logger.info(
                "Resolution of %s cancelled %d in-flight investigation job(s): %s",
                incident_id,
                len(cancelled),
                ", ".join(str(job_id) for job_id in cancelled),
            )
    except Exception as cancel_err:
        logger.warning(
            "Could not cancel investigations for resolved incident %s: %s",
            incident_id,
            cancel_err,
        )
    if cancelled:
        # Slack is the only channel this platform has, and the thread the
        # on-call is watching currently reads ":rotating_light: Incident
        # opened" and nothing else. Stopping the work silently is the same
        # failure as failing silently: the reader cannot tell a cancelled
        # investigation from one still thinking. Only sent when something was
        # actually stopped — saying "stopped" about nothing is its own lie.
        notified = False
        try:
            from .war_room_service import post_to_incident_thread

            notified = await post_to_incident_thread(
                incident_id,
                f":white_check_mark: `{incident.title}` is resolved, so the "
                "investigation that was still running for it has been stopped. "
                "No approval will be requested for this incident.",
            )
        except Exception as notify_err:
            logger.warning(
                "Cancellation notice failed for incident %s: %s",
                incident_id,
                notify_err,
            )
        if not notified:
            logger.error(
                "Incident %s resolved and its investigation cancelled, but NO "
                "Slack notice was delivered to the thread",
                incident_id,
            )
    try:
        from .war_room_service import close_war_room

        await close_war_room(incident_id)
    except Exception:
        pass
    try:
        from .live_events import publish_lifecycle_event

        await publish_lifecycle_event(
            "resolved",
            incident_id=incident_id,
            alert_name=incident.title,
            summary=incident.summary or "",
            org_id=str(organization_id),
            status=str(models.IncidentStatus.RESOLVED),
        )
    except Exception:
        pass
    try:
        from .integrations.jira import transition_jira_issue

        await transition_jira_issue(
            incident_id, str(cluster_id), str(models.IncidentStatus.RESOLVED)
        )
    except Exception:
        pass


async def mark_incident_resolved_by_human(
    *,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
) -> Optional[Any]:
    """Close an incident the pipeline itself can never close.

    ``acknowledge_incident_resolution`` only accepts PENDING_ACKNOWLEDGMENT —
    the state a verified autonomous fix lands in. Every other terminal state
    (INVESTIGATED after the agent paged a human, VERIFICATION_UNKNOWN,
    REMEDIATION_FAILED) has no automated way forward, and dedup keeps
    collapsing the re-firing alert into that incident for as long as it stays
    open, so without this an escalated incident suppresses its own alert
    forever. The authority here is a human who says they handled it, which is
    why there is deliberately no precondition on the current status — the same
    rule the dashboard's mark-resolved action has always used.

    Returns None if no incident matches the ownership scope (caller treats
    that as 404). Already-resolved is a no-op, not an error.
    """
    from sqlalchemy import update

    from backend import database, models

    incident_uuid = uuid.UUID(str(incident_id))
    organization_uuid = uuid.UUID(str(organization_id))
    cluster_uuid = uuid.UUID(str(cluster_id))
    now = utc_now()

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, incident_uuid)
        if incident is None or str(incident.cluster_id) != str(cluster_uuid):
            return None
        cluster = await db.get(models.Cluster, cluster_uuid)
        if cluster is None or str(cluster.org_id) != str(organization_uuid):
            return None
        if incident.status == models.IncidentStatus.RESOLVED:
            return incident

        cas = await db.execute(
            update(models.Incident)
            .where(
                models.Incident.id == incident_uuid,
                models.Incident.status != models.IncidentStatus.RESOLVED,
            )
            .values(status=models.IncidentStatus.RESOLVED, resolved_at=now)
        )
        if cas.rowcount != 1:
            # Someone else resolved it between the read and the write.
            await db.rollback()
            await db.refresh(incident)
            return incident
        await db.commit()
        await db.refresh(incident)

    await fire_resolution_side_effects(incident, organization_id, cluster_id)
    return incident


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


# ── The LangGraph Act-phase interrupt gate (high-risk/critical remediation) ─
#
# Distinct from the Temporal gates above: this is a single durable
# ApprovalRequest keyed off (incident_id, thread_id, action_hash), decided by
# resuming the paused LangGraph run directly (langgraph.types.Command). Was
# dashboard-only (mission_control.approve_incident_action); this pair lets a
# Slack "approve fix" reply do the exact same thing.


async def find_latest_pending_action_approval(*, incident_id: str) -> Optional[str]:
    """Return the id of the newest PENDING ApprovalRequest for this incident,
    or None. Slack's "approve fix" reply only knows the incident, not the
    approval_request_id/action_hash the dashboard has from its own GET
    /status call, so it resolves the id through here first.
    """
    from sqlalchemy import select

    from backend import database, models

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.ApprovalRequest.id)
            .where(
                models.ApprovalRequest.incident_id == uuid.UUID(str(incident_id)),
                models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            )
            .order_by(models.ApprovalRequest.created_at.desc())
            .limit(1)
        )
        row_id = result.scalar_one_or_none()
        return str(row_id) if row_id is not None else None


async def decide_action_approval(
    *,
    approval_request_id: str,
    incident_id: str,
    organization_id: str,
    cluster_id: str,
    approver_user_id: str,
) -> Optional[str]:
    """Authorize and synchronously resume the one exact graph action pending
    on this ApprovalRequest — the same CAS-then-resume mission_control's
    dashboard endpoint performs, factored out so Slack can trigger it too.

    Returns the incident's newly computed status on success, or None if no
    ApprovalRequest matches the ownership scope (caller treats that as 404).
    Raises ApprovalValidationError("not_pending" | "expired" | "hash_mismatch")
    for the caller to translate into a user-facing message.
    """
    from sqlalchemy import select, update

    from backend import database, models
    from sre_agent.checkpointer import durable_checkpointer_configured

    if not durable_checkpointer_configured():
        raise RuntimeError("A durable checkpointer is required for approvals")

    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.id == uuid.UUID(str(approval_request_id)),
                models.ApprovalRequest.incident_id == uuid.UUID(str(incident_id)),
                models.ApprovalRequest.organization_id == uuid.UUID(str(organization_id)),
                models.ApprovalRequest.cluster_id == uuid.UUID(str(cluster_id)),
            )
        )
        pending = result.scalar_one_or_none()
        if pending is None:
            return None

        now = utc_now()
        # An approver reaching this line has already had their Slack identity
        # and admin role freshly re-verified for *this* decide call — expiry
        # is a clock papercut on the plan proposal, not a re-authorization
        # requirement, so a still-pending (never approved/rejected) request
        # that has merely gone stale is renewed rather than refused. This
        # only fires while status is still "pending": an already
        # approved/rejected/expired-and-superseded row is untouched.
        if pending.status == models.ApprovalStatus.PENDING and is_expired(pending.expires_at, now):
            pending.expires_at = now + approval_ttl()
            await db.flush()

        # Slack's "approve fix" only names the incident, not the action hash —
        # it trusts whichever plan is currently pending rather than requiring
        # the hash to be retyped, so submitted == stored here by construction.
        validate_pending_approval(
            status=pending.status,
            stored_action_hash=pending.action_hash,
            submitted_action_hash=pending.action_hash,
            expires_at=pending.expires_at,
            now=now,
        )

        from sre_agent.agent_runtime import get_agent_runtime
        from sre_agent.checkpointer import thread_config

        try:
            runtime = await get_agent_runtime(uuid.UUID(str(cluster_id)))
        except Exception as exc:
            raise RuntimeError("Agent system unavailable") from exc
        graph = runtime.graph
        from sre_agent import tracing

        org_langfuse = runtime.context.org_langfuse_credentials()
        # Same Langfuse session as the investigation that produced this plan
        # (the incident id), so the tracing UI shows the whole human-in-the-loop
        # workflow in order: investigate → Slack approval → remediate.
        config = thread_config(
            pending.thread_id,
            {
                "metadata": tracing.trace_attributes(
                    "resume-remediation",
                    context=runtime.context,
                    session_id=str(incident_id),
                    user_id=str(approver_user_id),
                    trigger="slack-approval",
                    metadata={
                        "incident_id": str(incident_id),
                        "approval_request_id": str(pending.id),
                        "action_hash": pending.action_hash,
                    },
                ),
            },
            org_langfuse=org_langfuse,
        )
        configurable = (config or {}).get("configurable", {})
        if configurable.get("thread_id") != pending.thread_id:
            raise RuntimeError("Durable checkpointing is required for approvals")

        try:
            snapshot = await graph.aget_state(config)
        except Exception as exc:
            raise RuntimeError("Pending graph interrupt unavailable") from exc

        interrupt_payload = current_approval_interrupt(snapshot)
        if not interrupt_payload:
            raise ApprovalValidationError("not_pending")
        interrupt_report = interrupt_payload.get("report")
        if not isinstance(interrupt_report, dict):
            raise ApprovalValidationError("not_pending")
        current_hash = compute_action_hash(interrupt_report)
        if (
            str(interrupt_payload.get("approval_request_id")) != str(pending.id)
            or str(interrupt_payload.get("thread_id")) != pending.thread_id
            or not secrets.compare_digest(
                str(interrupt_payload.get("action_hash", "")), pending.action_hash
            )
            or not secrets.compare_digest(current_hash, pending.action_hash)
        ):
            raise ApprovalValidationError("hash_mismatch")

        cas = await db.execute(
            update(models.ApprovalRequest)
            .where(
                models.ApprovalRequest.id == pending.id,
                models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            )
            .values(
                status=models.ApprovalStatus.APPROVED,
                approver_user_id=uuid.UUID(str(approver_user_id)),
                decided_at=now,
            )
        )
        if cas.rowcount != 1:
            await db.rollback()
            raise ApprovalValidationError("not_pending")
        await db.commit()

    from langgraph.types import Command
    from .redis_state_store import get_state_store

    state_store = get_state_store()
    session_id = str(incident_id)

    # Stream (not a single ainvoke) so the redis-backed live status that
    # Slack/dashboard "what's happening right now" questions read
    # (state_store, keyed by incident id — see agent_runtime.py's
    # _run_graph_impl) keeps advancing through the post-approval remediation
    # nodes too, instead of going stale the moment the graph resumes.
    output: Dict[str, Any] = {}
    try:
        async with tracing.trace_run(
            "resume-remediation",
            org_langfuse=org_langfuse,
            input={
                "approved_plan": (interrupt_report or {}).get("actions")
                or interrupt_report,
                "approved_by": str(approver_user_id),
                "incident_id": str(incident_id),
            },
            metadata={
                "incident_id": str(incident_id),
                "approval_request_id": str(pending.id),
            },
        ) as traced_run:
            async for event in graph.astream(
                Command(
                    resume={
                        "approved": True,
                        "approval_request_id": str(pending.id),
                        "action_hash": pending.action_hash,
                    }
                ),
                config=config,
            ):
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict):
                        output = {**output, **node_output}
                    state_store.set(
                        session_id,
                        {
                            "status": "RUNNING",
                            "current_node": node_name,
                            "timestamp": utc_now().isoformat(),
                        },
                        ttl=3600,
                    )
            traced_run.set_output(
                {
                    "act_report": (output.get("metadata") or {}).get("act_report"),
                    "summary": output.get("final_response"),
                }
            )
    except Exception as exc:
        state_store.set(session_id, {"status": "ERROR", "error": str(exc)}, ttl=3600)
        raise RuntimeError("Approved action failed to resume") from exc

    # Terminal live state. Without this the last thing written is the final
    # node's "RUNNING", so "what's happening right now" keeps answering with a
    # node that finished minutes ago until the 3600s TTL expires.
    state_store.set(
        session_id,
        {
            "status": "COMPLETED",
            "current_node": None,
            "timestamp": utc_now().isoformat(),
        },
        ttl=3600,
    )

    if not isinstance(output, dict) or not output:
        return None

    from sre_agent.incident_status import compute_incident_status, resolved_at_for_status

    act_report = (output.get("metadata") or {}).get("act_report")
    verification = (act_report or {}).get("verification")
    computed_status = compute_incident_status(output, act_report, verification)
    # Written unconditionally, both directions: an Alertmanager *resolved*
    # webhook can stamp the row while this approved run is still verifying,
    # and then the run computes REMEDIATION_FAILED. Setting only the status
    # left `remediation_failed` rows carrying a `resolved_at`, which MTTR
    # counts as a fast resolution (see incident_status.resolved_at_for_status).
    incident_values: Dict[str, Any] = {
        "status": computed_status,
        "resolved_at": resolved_at_for_status(computed_status, utc_now()),
    }

    async with database.AsyncSessionLocal() as db:
        await db.execute(
            update(models.Incident)
            .where(models.Incident.id == uuid.UUID(str(incident_id)))
            .values(**incident_values)
        )
        await db.commit()

    return computed_status
