#!/usr/bin/env python3
"""
War room — the two-way incident conversation (design slice #2).

Turns "the system posts a message" into "on-call and the agent converse". Each
incident gets a dedicated Slack thread (the war room). The agent *streams* its
work into the thread (outbound, off the live event bus from slice #1), and
on-call *replies in the thread* (inbound), which routes directly through the
same memory-backed conversational handler the dashboard chat uses
(``mission_control.handle_incident_message``) — so a human message becomes a
real, remembered turn in the incident's conversation, not a one-off keyword
match.

Framework-agnostic and testable: Slack I/O is injected as a ``poster`` and a
``handler``. The live Slack wiring lives in ``integrations/slack_bot.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .live_events import get_event_bus, incident_channel

logger = logging.getLogger(__name__)

# Control channel: incident lifecycle events (opened/closed) the Slack service
# listens on to create/close war rooms.
INCIDENTS_CHANNEL = "incidents"

# Timeline event types worth surfacing to humans in the thread (the rest is noise).
# `approval` is not noise by definition: it is the one event the thread exists to
# carry — the agent asking a human for authorization it cannot grant itself.
_SURFACED = {"plan", "decision", "summary", "act", "assistant_message", "approval"}


@dataclass(frozen=True)
class ThreadRef:
    channel: str
    thread_ts: str

    def key(self) -> str:
        return f"{self.channel}:{self.thread_ts}"


class WarRoomRegistry:
    """Bidirectional incident_id ↔ Slack thread mapping."""

    def __init__(self) -> None:
        self._by_incident: Dict[str, ThreadRef] = {}
        self._by_thread: Dict[str, str] = {}

    def open(self, incident_id: str, thread: ThreadRef) -> None:
        self._by_incident[incident_id] = thread
        self._by_thread[thread.key()] = incident_id

    def thread_for(self, incident_id: str) -> Optional[ThreadRef]:
        return self._by_incident.get(incident_id)

    def incident_for(self, thread: ThreadRef) -> Optional[str]:
        return self._by_thread.get(thread.key())

    def is_war_room(self, thread: ThreadRef) -> bool:
        return thread.key() in self._by_thread

    def close(self, incident_id: str) -> None:
        thread = self._by_incident.pop(incident_id, None)
        if thread:
            self._by_thread.pop(thread.key(), None)


def format_event_for_slack(event: Dict[str, Any]) -> Optional[str]:
    """Turn a live-bus event into a Slack message, or None to skip (noise)."""
    if event.get("type") != "timeline":
        return None
    p = event.get("payload", {}) or {}
    if p.get("event_type") not in _SURFACED:
        return None
    title = p.get("title") or p.get("speaker_role") or "Agent"
    content = str(p.get("content", "")).strip()
    return f"*{title}*\n{content[:1500]}" if content else f"*{title}*"


def _format_result_for_reply(result: Dict[str, Any]) -> str:
    """Turn a `handle_incident_message` status dict into a Slack reply."""
    status = result.get("status")
    if status == "RESPONDED":
        return result.get("response") or "Got it."
    if status == "PENDING_SUPERVISOR":
        return "Got it — queued for the next safe supervisor checkpoint."
    if status in ("FOLLOW_UP_QUEUED", "QUEUED"):
        return "On it — I'll follow up in this thread once that's done."
    return "Sorry, I couldn't process that."


async def _default_handler(
    text: str, incident_id: str, asker_email: Optional[str] = None
) -> Dict[str, Any]:
    """Route an in-thread reply through the real, memory-backed conversational
    endpoint the dashboard already uses — in-process, no HTTP hop.

    ``asker_email`` is the replying Slack user's profile email, the only
    identity bridge Slack gives us (same one the gate commands authorize on).
    It is resolved to a platform user purely for *attribution* — who asked —
    so the answer is never withheld when the lookup comes back empty.
    """
    import uuid as _uuid

    from backend import crud, database, models
    from sre_agent.api.v1.mission_control import handle_incident_message

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is None:
            return {"status": "ignored"}
        cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
        user_id = await _platform_user_id_for_email(db, asker_email, cluster)
        return await handle_incident_message(
            db, incident, cluster, text, source="slack", user_id=user_id
        )


async def _platform_user_id_for_email(
    db: Any, email: Optional[str], cluster: Any
) -> Optional[str]:
    """Map a Slack profile email to this org's platform user id, or None.

    Org-scoped on purpose: an email that matches a user in a *different*
    tenant must not be credited with asking this org's question. Never raises
    — attribution is metadata, and a question over Slack (the only channel)
    must still be answered when identity can't be established.
    """
    if not email or cluster is None:
        return None
    try:
        from sqlalchemy import select

        from backend import models

        result = await db.execute(select(models.User).where(models.User.email == email))
        user = result.scalar_one_or_none()
        if user is None or str(user.org_id) != str(getattr(cluster, "org_id", "")):
            return None
        return str(user.id)
    except Exception as exc:  # pragma: no cover - attribution is best-effort
        logger.debug("war-room: could not attribute Slack reply to a user: %s", exc)
        return None


async def forward_events(
    incident_id: str,
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
    bus=None,
    registry: Optional[WarRoomRegistry] = None,
    max_events: Optional[int] = None,
) -> int:
    """Stream an incident's bus events into its Slack thread (outbound). Long-running.

    Returns the number of events processed (bounded by ``max_events`` in tests).
    """
    bus = bus or get_event_bus()
    sub = bus.subscribe(incident_channel(incident_id))
    processed = 0
    try:
        async for event in sub:
            text = format_event_for_slack(event)
            if text:
                thread = registry.thread_for(incident_id) if registry else None
                await poster(thread, text)
            processed += 1
            if max_events is not None and processed >= max_events:
                break
    finally:
        sub.close()
    return processed


def _normalize_command_text(text: str) -> str:
    """Strip the client-side noise that would otherwise defeat an exact
    command match: a Slack @mention prefix (<@U123>), markdown emphasis
    (*bold*/_italic_/`code`) a client may add around the command, and
    trailing punctuation mobile keyboards like to auto-append ("approve
    fix."). Anchored full-string regexes on the raw text are too brittle for
    a channel real users type into — this is the single normalization point
    every command matcher below runs through.
    """
    text = text or ""
    text = re.sub(r"<@[^>]+>", "", text)
    text = text.strip().strip("*_~`")
    text = text.rstrip(".!?,;: \t\n")
    return text.strip()


GATE_COMMAND_RE = re.compile(
    r"^(approve|deny)\s+(start[-_]fix|raise[-_]pr|retry[-_]fix|close[-_]incident)$",
    re.IGNORECASE,
)

# Distinct from GATE_COMMAND_RE's four Temporal-signaled gates: acknowledging
# a resolution doesn't signal a running workflow (verification already ran
# and there's nothing left waiting) — it just flips the incident's own
# PENDING_ACKNOWLEDGMENT status to RESOLVED. See acknowledge_incident_resolution
# in approval_flow.py.
ACK_COMMAND_RE = re.compile(r"^(?:acknowledge|ack)(?:\s+resolution)?$", re.IGNORECASE)

# The way out for every incident "acknowledge" can't close. Acknowledging only
# applies to PENDING_ACKNOWLEDGMENT — a verified autonomous fix — so an
# incident the agent escalated (INVESTIGATED), or one whose verification came
# back FAILED/UNKNOWN, had no Slack path to resolution at all, and dedup keeps
# folding the re-firing alert into it while it stays open. Spelled without a
# Deliberately does not spell "close incident": that reads as GATE_COMMAND_RE's
# "approve close-incident" Temporal gate, and the two mean different things.
RESOLVE_COMMAND_RE = re.compile(
    r"^(?:mark\s+resolved|resolve\s+incident)$", re.IGNORECASE
)


def is_ack_command(text: str) -> bool:
    return bool(ACK_COMMAND_RE.match(_normalize_command_text(text)))


def is_resolve_command(text: str) -> bool:
    return bool(RESOLVE_COMMAND_RE.match(_normalize_command_text(text)))


def parse_gate_command(text: str) -> Optional[tuple]:
    """Parse an in-thread reply like "approve start-fix" / "deny raise_pr"
    into (gate, approved), or None if the text isn't a gate decision. Pure —
    no Slack, no DB — so it's unit-testable the same way format_reply and
    format_event_for_slack are.
    """
    match = GATE_COMMAND_RE.match(_normalize_command_text(text))
    if not match:
        return None
    verb, gate_raw = match.group(1).lower(), match.group(2).lower().replace("-", "_")
    return gate_raw, verb == "approve"


async def route_gate_command(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    approver_email: Optional[str],
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
) -> Optional[Dict[str, Any]]:
    """Decide one of Phase 5's two remediation gates from an in-thread Slack
    reply ("approve start-fix" / "deny raise-pr"). Returns None (caller
    should fall back to route_thread_reply) if `text` isn't a gate command;
    otherwise decides it and posts the outcome, mirroring
    sre_agent/api/v1/remediation_gates.py's dashboard path but authorizing
    off the replying Slack user's email instead of a JWT.
    """
    parsed = parse_gate_command(text)
    if parsed is None:
        return None

    gate, approved = parsed
    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    result = await _decide_gate_for_incident(incident_id, gate, approved, approver_email)
    await poster(thread, result["message"])
    return result


async def _decide_gate_for_incident(
    incident_id: str, gate: str, approved: bool, approver_email: Optional[str]
) -> Dict[str, Any]:
    import uuid as _uuid

    from sqlalchemy import select

    from backend import crud, database, models

    from .approval_flow import (
        ApprovalValidationError,
        decide_and_signal_gate,
        find_latest_pending_gate,
    )

    if not approver_email:
        return {"mode": "gate_decision", "status": "denied", "message": "Couldn't verify your Slack identity — no email on file."}

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is None:
            return {"mode": "ignored"}
        cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
        if cluster is None:
            return {"mode": "ignored"}

        user_result = await db.execute(
            select(models.User).where(models.User.email == approver_email)
        )
        approver = user_result.scalar_one_or_none()

    if approver is None or str(approver.org_id) != str(cluster.org_id):
        return {
            "mode": "gate_decision",
            "status": "denied",
            "message": f"{approver_email} isn't a member of this organization — can't decide this gate here.",
        }
    if approver.role != models.UserRole.ADMIN:
        return {
            "mode": "gate_decision",
            "status": "denied",
            "message": "Only admins can decide remediation gates.",
        }

    gate_approval_id = await find_latest_pending_gate(incident_id=incident_id, gate=gate)
    if gate_approval_id is None:
        return {
            "mode": "gate_decision",
            "status": "not_found",
            "message": f"No pending `{gate}` gate for this incident right now.",
        }

    try:
        row, delivered = await decide_and_signal_gate(
            gate_approval_id=gate_approval_id,
            incident_id=incident_id,
            organization_id=str(cluster.org_id),
            cluster_id=str(incident.cluster_id),
            approved=approved,
            approver_user_id=str(approver.id),
            approver_label=approver.email,
        )
    except ApprovalValidationError as exc:
        detail = "already decided" if exc.reason == "not_pending" else "expired"
        return {"mode": "gate_decision", "status": exc.reason, "message": f"That gate is {detail}."}

    if row is None:
        return {"mode": "gate_decision", "status": "not_found", "message": "Gate approval not found."}
    if not delivered:
        return {
            "mode": "gate_decision",
            "status": "recorded_not_delivered",
            "message": f"Recorded {'approval' if approved else 'denial'} of `{gate}`, but the workflow couldn't be signaled yet.",
        }

    verb = "Approved" if approved else "Denied"
    return {"mode": "gate_decision", "status": "ok", "message": f"✅ {verb} `{gate}` — thanks {approver.email}."}


FIX_APPROVAL_COMMAND_RE = re.compile(r"^approve\s+fix$", re.IGNORECASE)

# Fixed, exact-match set of common ways an on-call engineer signals approval
# intent without typing the literal command — "approved", "go ahead", "lgtm",
# etc. Matched deterministically against a known set, NOT a substring/keyword
# scan over free text, so it can't misfire on an unrelated sentence that
# happens to contain the word "approve". When one of these is seen instead of
# the exact command, the caller should ask for the exact phrase rather than
# falling through to the LLM chat path, which has no structural signal for
# whether an approval actually happened and will narrate a plausible-sounding
# but false confirmation if it's allowed to answer freely.
_APPROVAL_INTENT_NEAR_MISSES = {
    "approve", "approved", "yes approve", "please approve", "approve it",
    "approve the fix", "approve plan", "approve the plan", "approve remediation",
    "go ahead", "do it", "lgtm", "approved fix", "ok approve", "yes, approve",
}


def is_fix_approval_command(text: str) -> bool:
    return bool(FIX_APPROVAL_COMMAND_RE.match(_normalize_command_text(text)))


def is_approval_intent_near_miss(text: str) -> bool:
    """True for a reply that clearly means to approve the pending fix but
    isn't the exact required command — see _APPROVAL_INTENT_NEAR_MISSES."""
    return _normalize_command_text(text).lower() in _APPROVAL_INTENT_NEAR_MISSES


async def route_fix_approval_command(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    approver_email: Optional[str],
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
) -> Optional[Dict[str, Any]]:
    """Approve the pending high-risk remediation — the LangGraph Act-phase
    interrupt gate — from an in-thread "approve fix" reply. This is the Slack
    equivalent of the dashboard's former "Approve & run" button; there is no
    "deny fix" because the button it replaces never had one either (denying a
    high-risk plan just means leaving the incident paused). Returns None
    (caller falls back to route_thread_reply) if `text` isn't this command.
    """
    if not is_fix_approval_command(text):
        return None

    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    result = await _decide_action_approval_for_incident(incident_id, approver_email)
    await poster(thread, result["message"])
    return result


async def _decide_action_approval_for_incident(
    incident_id: str, approver_email: Optional[str]
) -> Dict[str, Any]:
    import uuid as _uuid

    from sqlalchemy import select

    from backend import crud, database, models

    from .approval_flow import (
        ApprovalValidationError,
        decide_action_approval,
        find_latest_pending_action_approval,
    )

    if not approver_email:
        return {"mode": "action_decision", "status": "denied", "message": "Couldn't verify your Slack identity — no email on file."}

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is None:
            return {"mode": "ignored"}
        cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
        if cluster is None:
            return {"mode": "ignored"}

        user_result = await db.execute(
            select(models.User).where(models.User.email == approver_email)
        )
        approver = user_result.scalar_one_or_none()

    if approver is None or str(approver.org_id) != str(cluster.org_id):
        return {
            "mode": "action_decision",
            "status": "denied",
            "message": f"{approver_email} isn't a member of this organization — can't approve this here.",
        }
    if approver.role != models.UserRole.ADMIN:
        return {
            "mode": "action_decision",
            "status": "denied",
            "message": "Only admins can approve remediations.",
        }

    approval_request_id = await find_latest_pending_action_approval(incident_id=incident_id)
    if approval_request_id is None:
        return {
            "mode": "action_decision",
            "status": "not_found",
            "message": "No pending remediation approval for this incident right now.",
        }

    try:
        computed_status = await decide_action_approval(
            approval_request_id=approval_request_id,
            incident_id=incident_id,
            organization_id=str(cluster.org_id),
            cluster_id=str(incident.cluster_id),
            approver_user_id=str(approver.id),
        )
    except ApprovalValidationError as exc:
        detail = {
            "not_pending": "already decided",
            "expired": "expired",
            "hash_mismatch": "no longer matches the current plan",
        }.get(exc.reason, exc.reason)
        return {"mode": "action_decision", "status": exc.reason, "message": f"That approval is {detail}."}
    except Exception:
        logger.exception("approve fix: resume failed for incident %s", incident_id)
        return {
            "mode": "action_decision",
            "status": "error",
            "message": "Approved, but the remediation failed to resume — check the incident page.",
        }

    if computed_status is None:
        return {"mode": "action_decision", "status": "not_found", "message": "Approval request not found."}

    return {
        "mode": "action_decision",
        "status": "ok",
        "message": f"✅ Approved — remediation is running. ({approver.email})",
    }


async def route_ack_command(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    approver_email: Optional[str],
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
) -> Optional[Dict[str, Any]]:
    """Handle an in-thread "acknowledge" reply confirming a verified fix is
    actually done. Returns None (caller should fall back to route_gate_command
    / route_thread_reply) if `text` isn't an ack command.

    This is the human sign-off the user asked for: verification succeeding
    is not enough to call an incident RESOLVED on its own anymore — see
    sre_agent.incident_status.compute_incident_status, which now stops at
    PENDING_ACKNOWLEDGMENT. Only an admin's "acknowledge" here (or the
    equivalent dashboard action) advances it to RESOLVED.
    """
    if not is_ack_command(text):
        return None

    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    result = await _acknowledge_resolution_for_incident(incident_id, approver_email)
    await poster(thread, result["message"])
    return result


async def route_resolve_command(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    approver_email: Optional[str],
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
) -> Optional[Dict[str, Any]]:
    """Handle an in-thread "mark resolved" reply closing an incident a human
    took over — the Slack equivalent of the dashboard's mark-resolved action.

    Distinct from "acknowledge", which only works on a verified autonomous fix
    (PENDING_ACKNOWLEDGMENT). An incident the agent escalated, or one whose
    verification failed, could be ended from the dashboard but not from Slack,
    which is the only channel this design has — and while it stayed open,
    dedup folded every re-firing alert into it instead of opening a new
    incident.
    """
    if not is_resolve_command(text):
        return None

    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    result = await _mark_resolved_for_incident(incident_id, approver_email)
    await poster(thread, result["message"])
    return result


async def _authorize_incident_admin(
    incident_id: str, approver_email: Optional[str], action: str
) -> Dict[str, Any]:
    """Resolve the Slack replier to an admin of the incident's own org.

    Returns ``{"ok": True, "incident": ..., "cluster": ..., "approver": ...}``
    or a ready-to-post refusal in the same shape the routers return.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from backend import crud, database, models

    mode = f"{action}_decision"
    if not approver_email:
        return {
            "mode": mode,
            "status": "denied",
            "message": "Couldn't verify your Slack identity — no email on file.",
        }

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is None:
            return {"mode": "ignored"}
        cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
        if cluster is None:
            return {"mode": "ignored"}

        user_result = await db.execute(
            select(models.User).where(models.User.email == approver_email)
        )
        approver = user_result.scalar_one_or_none()

    if approver is None or str(approver.org_id) != str(cluster.org_id):
        return {
            "mode": mode,
            "status": "denied",
            "message": f"{approver_email} isn't a member of this organization — can't {action} this incident here.",
        }
    if approver.role != models.UserRole.ADMIN:
        return {
            "mode": mode,
            "status": "denied",
            "message": f"Only admins can {action} an incident.",
        }

    return {"ok": True, "incident": incident, "cluster": cluster, "approver": approver}


async def _mark_resolved_for_incident(
    incident_id: str, approver_email: Optional[str]
) -> Dict[str, Any]:
    from .approval_flow import mark_incident_resolved_by_human

    authorized = await _authorize_incident_admin(incident_id, approver_email, "resolve")
    if not authorized.get("ok"):
        return authorized

    incident = authorized["incident"]
    cluster = authorized["cluster"]
    approver = authorized["approver"]

    resolved = await mark_incident_resolved_by_human(
        incident_id=incident_id,
        organization_id=str(cluster.org_id),
        cluster_id=str(incident.cluster_id),
    )
    if resolved is None:
        return {"mode": "resolve_decision", "status": "not_found", "message": "Incident not found."}

    return {
        "mode": "resolve_decision",
        "status": "ok",
        "message": (
            f"✅ Incident marked resolved by {approver.email}. "
            "The agent did not verify a fix — this is your call that it's handled."
        ),
    }


async def _acknowledge_resolution_for_incident(
    incident_id: str, approver_email: Optional[str]
) -> Dict[str, Any]:
    from .approval_flow import ApprovalValidationError, acknowledge_incident_resolution

    authorized = await _authorize_incident_admin(
        incident_id, approver_email, "acknowledge"
    )
    if not authorized.get("ok"):
        return authorized

    incident = authorized["incident"]
    cluster = authorized["cluster"]
    approver = authorized["approver"]

    try:
        resolved = await acknowledge_incident_resolution(
            incident_id=incident_id,
            organization_id=str(cluster.org_id),
            cluster_id=str(incident.cluster_id),
        )
    except ApprovalValidationError:
        return {
            "mode": "ack_decision",
            "status": "not_pending",
            "message": (
                "This incident isn't awaiting acknowledgment right now — that "
                "applies only to a fix the agent verified. If you've handled it "
                "yourself, reply `mark resolved` to close it."
            ),
        }

    if resolved is None:
        return {"mode": "ack_decision", "status": "not_found", "message": "Incident not found."}

    return {
        "mode": "ack_decision",
        "status": "ok",
        "message": f"✅ Resolution acknowledged by {approver.email} — incident marked resolved.",
    }


async def route_thread_reply(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
    handler: Optional[
        Callable[[str, str, Optional[str]], Awaitable[Dict[str, Any]]]
    ] = None,
    asker_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Route an on-call reply in a war-room thread (inbound) through the real,
    memory-backed conversational handler (`mission_control.handle_incident_message`
    in production; injectable for tests). Ignores replies in threads that
    aren't war rooms.

    `asker_email` carries the replying Slack user's profile email through to
    the handler, the way the gate commands already carry the approver's: over
    Slack — the only channel — an unattributed follow-up is a trace with no
    `userId`, and no way to tell who asked what.
    """
    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    handler = handler or _default_handler
    result = await handler(text, incident_id, asker_email)
    await poster(thread, _format_result_for_reply(result))
    return result
