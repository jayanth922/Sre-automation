#!/usr/bin/env python3
"""The Slack reply that closes an incident the agent could not close itself.

`acknowledge` only advances PENDING_ACKNOWLEDGMENT — a fix the agent verified.
An incident it escalated to a human (INVESTIGATED), or one whose verification
came back FAILED/UNKNOWN, had no Slack path to resolution at all, and dedup
folds every re-firing alert into it while it stays open. Slack is the only
channel in this design, so that was a one-way door.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent import war_room as wr  # noqa: E402


def test_resolve_command_recognises_the_phrasings_a_human_types():
    assert wr.is_resolve_command("mark resolved")
    assert wr.is_resolve_command("  *Mark Resolved*  ")
    assert wr.is_resolve_command("resolve incident.")
    assert not wr.is_resolve_command("acknowledge")
    assert not wr.is_resolve_command("is this resolved?")
    # "close incident" belongs to the Temporal gate command, not to this.
    assert not wr.is_resolve_command("close incident")
    assert wr.parse_gate_command("approve close-incident") == ("close_incident", True)


def _thread_and_registry():
    registry = wr.WarRoomRegistry()
    thread = wr.ThreadRef("C1", "111.222")
    registry.open("inc-1", thread)
    return thread, registry


def _route(monkeypatch, authorized, marker, text="mark resolved"):
    thread, registry = _thread_and_registry()
    posted = []

    async def _authorize(incident_id, approver_email, action):
        return authorized

    monkeypatch.setattr(wr, "_authorize_incident_admin", _authorize)
    monkeypatch.setitem(
        sys.modules,
        "sre_agent.approval_flow",
        SimpleNamespace(mark_incident_resolved_by_human=marker),
    )

    async def poster(_thread, text):
        posted.append(text)

    result = asyncio.run(
        wr.route_resolve_command(text, thread, registry, "sre@example.com", poster)
    )
    return result, posted


def test_an_admin_can_close_an_incident_the_agent_only_escalated(monkeypatch):
    calls = {}

    async def marker(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(id="inc-1")

    authorized = {
        "ok": True,
        "incident": SimpleNamespace(cluster_id="cluster-1"),
        "cluster": SimpleNamespace(org_id="org-1"),
        "approver": SimpleNamespace(email="sre@example.com"),
    }
    result, posted = _route(monkeypatch, authorized, marker)

    assert result["status"] == "ok"
    assert calls == {
        "incident_id": "inc-1",
        "organization_id": "org-1",
        "cluster_id": "cluster-1",
    }
    # The reply must not imply the agent verified anything.
    assert "did not verify" in posted[0]
    assert "sre@example.com" in posted[0]


def test_a_non_admin_is_refused_and_nothing_is_resolved(monkeypatch):
    async def marker(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("a refused replier must not resolve the incident")

    refusal = {
        "mode": "resolve_decision",
        "status": "denied",
        "message": "Only admins can resolve an incident.",
    }
    result, posted = _route(monkeypatch, refusal, marker)
    assert result["status"] == "denied"
    assert posted == ["Only admins can resolve an incident."]


def test_other_replies_fall_through_to_the_normal_handler(monkeypatch):
    async def marker(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("not a resolve command")

    result, posted = _route(
        monkeypatch, {"ok": True}, marker, text="what's the p90 right now?"
    )
    assert result is None
    assert posted == []


def test_the_ack_dead_end_points_at_the_command_that_works(monkeypatch):
    """The message a human sees when "acknowledge" is refused has to name the
    way out, or the incident is unclosable from the only channel there is."""

    class _Invalid(Exception):
        pass

    async def _acknowledge(**kwargs):
        raise _Invalid()

    async def _authorize(incident_id, approver_email, action):
        return {
            "ok": True,
            "incident": SimpleNamespace(cluster_id="cluster-1"),
            "cluster": SimpleNamespace(org_id="org-1"),
            "approver": SimpleNamespace(email="sre@example.com"),
        }

    monkeypatch.setattr(wr, "_authorize_incident_admin", _authorize)
    monkeypatch.setitem(
        sys.modules,
        "sre_agent.approval_flow",
        SimpleNamespace(
            ApprovalValidationError=_Invalid,
            acknowledge_incident_resolution=_acknowledge,
        ),
    )

    result = asyncio.run(
        wr._acknowledge_resolution_for_incident("inc-1", "sre@example.com")
    )
    assert result["status"] == "not_pending"
    assert "mark resolved" in result["message"]


def test_the_slack_bot_routes_the_command_before_the_llm_chat_path():
    source = (
        Path(__file__).resolve().parents[1] / "sre_agent" / "integrations" / "slack_bot.py"
    ).read_text()
    assert "is_resolve_command" in source
    assert source.index("route_resolve_command(") < source.index("route_thread_reply(\n")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
