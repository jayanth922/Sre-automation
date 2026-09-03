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
_SURFACED = {"plan", "decision", "summary", "act", "assistant_message"}


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


async def _default_handler(text: str, incident_id: str) -> Dict[str, Any]:
    """Route an in-thread reply through the real, memory-backed conversational
    endpoint the dashboard already uses — in-process, no HTTP hop."""
    import uuid as _uuid

    from backend import crud, database, models
    from sre_agent.api.v1.mission_control import handle_incident_message

    async with database.AsyncSessionLocal() as db:
        incident = await db.get(models.Incident, _uuid.UUID(incident_id))
        if incident is None:
            return {"status": "ignored"}
        cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
        return await handle_incident_message(db, incident, cluster, text, source="slack")


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


GATE_COMMAND_RE = re.compile(
    r"^\s*(approve|deny)\s+(start[-_]fix|raise[-_]pr|retry[-_]fix|close[-_]incident)\s*$",
    re.IGNORECASE,
)


def parse_gate_command(text: str) -> Optional[tuple]:
    """Parse an in-thread reply like "approve start-fix" / "deny raise_pr"
    into (gate, approved), or None if the text isn't a gate decision. Pure —
    no Slack, no DB — so it's unit-testable the same way format_reply and
    format_event_for_slack are.
    """
    match = GATE_COMMAND_RE.match(text or "")
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

    if approver is None or str(approver.org_id) != str(incident.org_id):
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
            organization_id=str(incident.org_id),
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


async def route_thread_reply(
    text: str,
    thread: ThreadRef,
    registry: WarRoomRegistry,
    poster: Callable[[Optional[ThreadRef], str], Awaitable[Any]],
    handler: Optional[Callable[[str, str], Awaitable[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Route an on-call reply in a war-room thread (inbound) through the real,
    memory-backed conversational handler (`mission_control.handle_incident_message`
    in production; injectable for tests). Ignores replies in threads that
    aren't war rooms.
    """
    incident_id = registry.incident_for(thread)
    if not incident_id:
        return {"mode": "ignored"}

    handler = handler or _default_handler
    result = await handler(text, incident_id)
    await poster(thread, _format_result_for_reply(result))
    return result
