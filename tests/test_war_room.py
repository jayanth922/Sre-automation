#!/usr/bin/env python3
"""Unit tests for the two-way war room (design slice #2). In-memory + fakes."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "sre_agent"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


le = _load("live_events")
# war_room imports `from .live_events import ...`; load it as a package instead.
sys.path.insert(0, str(_BASE.parent))
from sre_agent.war_room import (  # noqa: E402
    ThreadRef,
    WarRoomRegistry,
    forward_events,
    format_event_for_slack,
    parse_gate_command,
    route_gate_command,
    route_thread_reply,
)
from sre_agent.live_events import InMemoryEventBus, publish_incident_event  # noqa: E402


# ── registry ─────────────────────────────────────────────────────────────────
def test_registry_bidirectional():
    reg = WarRoomRegistry()
    t = ThreadRef("C1", "111.222")
    reg.open("inc-1", t)
    assert reg.thread_for("inc-1") == t
    assert reg.incident_for(t) == "inc-1"
    assert reg.is_war_room(t)
    reg.close("inc-1")
    assert reg.thread_for("inc-1") is None
    assert not reg.is_war_room(t)


# ── event formatting ───────────────────────────────────────────────────────────
def test_format_surfaces_plan_skips_noise():
    plan = {"type": "timeline", "payload": {"event_type": "plan", "title": "Supervisor", "content": "Investigation plan…"}}
    noise = {"type": "timeline", "payload": {"event_type": "thought", "title": "x", "content": "y"}}
    insight = {"type": "insight", "payload": {"error_rate": 0.1}}
    assert "Supervisor" in format_event_for_slack(plan)
    assert format_event_for_slack(noise) is None
    assert format_event_for_slack(insight) is None


def test_format_surfaces_the_approval_ask():
    """Slack is the only channel: an approval the thread never sees is an
    approval that expires in silence, with the incident stalled on a human who
    was never asked."""
    approval = {
        "type": "timeline",
        "payload": {
            "event_type": "approval",
            "title": "Approval required",
            "content": "🔒 Approval required — severity *UNKNOWN*.\nReply `approve fix`…",
        },
    }
    text = format_event_for_slack(approval)
    assert text is not None
    assert "Approval required" in text
    assert "approve fix" in text


# ── outbound: bus → Slack thread ──────────────────────────────────────────────
def test_forward_events_posts_surfaced_events():
    async def scenario():
        bus = InMemoryEventBus()
        reg = WarRoomRegistry()
        reg.open("inc-1", ThreadRef("C1", "T1"))
        posts = []

        async def poster(thread, text):
            posts.append((thread, text))

        task = asyncio.create_task(forward_events("inc-1", poster, bus=bus, registry=reg, max_events=1))
        await asyncio.sleep(0.01)  # let it subscribe
        await publish_incident_event("inc-1", "timeline",
                                     {"event_type": "act", "title": "Executor", "content": "restarted pod"}, bus=bus)
        await asyncio.wait_for(task, timeout=1)
        return posts

    posts = asyncio.run(scenario())
    assert len(posts) == 1
    thread, text = posts[0]
    assert thread == ThreadRef("C1", "T1")
    assert "Executor" in text and "restarted pod" in text


# ── inbound: thread reply routing ─────────────────────────────────────────────
def test_route_responded_posts_reply_directly():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        posts = []

        async def poster(thread, text): posts.append(text)
        async def handler(text, incident_id, asker_email=None):
            return {"status": "RESPONDED", "incident_id": incident_id, "response": "Error rate is 3%."}

        result = await route_thread_reply("what's the error rate?", t, reg, poster, handler=handler)
        return result, posts

    result, posts = asyncio.run(scenario())
    assert result["status"] == "RESPONDED"
    assert posts == ["Error rate is 3%."]


def test_route_pending_supervisor_acks_queued():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        posts = []

        async def poster(thread, text): posts.append(text)
        async def handler(text, incident_id, asker_email=None): return {"status": "PENDING_SUPERVISOR"}

        await route_thread_reply("restart the pod", t, reg, poster, handler=handler)
        return posts

    posts = asyncio.run(scenario())
    assert posts and "next safe supervisor checkpoint" in posts[0]


def test_route_queued_acks_on_it():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        posts = []

        async def poster(thread, text): posts.append(text)
        async def handler(text, incident_id, asker_email=None): return {"status": "QUEUED"}

        await route_thread_reply("check the payments service too", t, reg, poster, handler=handler)
        return posts

    posts = asyncio.run(scenario())
    assert posts and "On it" in posts[0]


def test_route_ignores_non_war_room_thread():
    async def scenario():
        reg = WarRoomRegistry()  # empty
        posts = []
        async def poster(thread, text): posts.append(text)
        async def handler(text, incident_id, asker_email=None): raise AssertionError("should not handle")
        res = await route_thread_reply("hello", ThreadRef("C9", "T9"), reg, poster, handler=handler)
        return res, posts

    res, posts = asyncio.run(scenario())
    assert res["mode"] == "ignored" and posts == []


def test_route_thread_reply_carries_the_asker_identity():
    """Who asked has to reach the handler — it becomes the trace's userId.

    Slack is the only channel, so an unattributed follow-up is a follow-up
    nobody can be credited with.
    """
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        seen = {}

        async def poster(thread, text): pass

        async def handler(text, incident_id, asker_email=None):
            seen["asker_email"] = asker_email
            return {"status": "RESPONDED", "response": "ok"}

        await route_thread_reply(
            "why does this need approval?", t, reg, poster,
            handler=handler, asker_email="oncall@example.com",
        )
        return seen

    assert asyncio.run(scenario())["asker_email"] == "oncall@example.com"


class _FakeResult:
    def __init__(self, user): self._user = user
    def scalar_one_or_none(self): return self._user


class _FakeDB:
    def __init__(self, user): self._user = user
    async def execute(self, _stmt): return _FakeResult(self._user)


class _FakeUser:
    def __init__(self, user_id, org_id): self.id, self.org_id = user_id, org_id


class _FakeCluster:
    def __init__(self, org_id): self.org_id = org_id


def test_slack_email_attributes_to_a_user_in_this_org():
    from sre_agent.war_room import _platform_user_id_for_email

    user_id = asyncio.run(
        _platform_user_id_for_email(
            _FakeDB(_FakeUser("u-1", "org-1")), "oncall@example.com",
            _FakeCluster("org-1"),
        )
    )
    assert user_id == "u-1"


def test_slack_email_matching_another_tenant_attributes_to_nobody():
    """A matching email in a different org is not this org's asker."""
    from sre_agent.war_room import _platform_user_id_for_email

    user_id = asyncio.run(
        _platform_user_id_for_email(
            _FakeDB(_FakeUser("u-1", "org-2")), "oncall@example.com",
            _FakeCluster("org-1"),
        )
    )
    assert user_id is None


def test_unresolvable_slack_identity_still_answers_unattributed():
    """No email (missing users:read.email scope) must not block the answer."""
    from sre_agent.war_room import _platform_user_id_for_email

    assert asyncio.run(
        _platform_user_id_for_email(_FakeDB(None), None, _FakeCluster("org-1"))
    ) is None


# ── inbound: gate-decision commands (Phase 5D) ───────────────────────────────
def test_parse_gate_command_accepts_approve_and_deny():
    assert parse_gate_command("approve start-fix") == ("start_fix", True)
    assert parse_gate_command("deny raise_pr") == ("raise_pr", False)
    assert parse_gate_command("  APPROVE   RAISE-PR  ") == ("raise_pr", True)


def test_parse_gate_command_rejects_non_commands():
    assert parse_gate_command("what's the error rate?") is None
    assert parse_gate_command("approve") is None
    assert parse_gate_command("approve something-else") is None
    assert parse_gate_command("") is None


def test_route_gate_command_returns_none_for_non_gate_text():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)

        async def poster(thread, text):
            raise AssertionError("should not post: not a gate command")

        return await route_gate_command("what's the error rate?", t, reg, "oncall@example.com", poster)

    assert asyncio.run(scenario()) is None


def test_route_gate_command_ignores_non_war_room_thread():
    async def scenario():
        reg = WarRoomRegistry()  # empty
        posts = []

        async def poster(thread, text):
            posts.append(text)

        res = await route_gate_command("approve start-fix", ThreadRef("C9", "T9"), reg, "oncall@example.com", poster)
        return res, posts

    res, posts = asyncio.run(scenario())
    assert res["mode"] == "ignored" and posts == []


def test_route_gate_command_posts_decision_outcome(monkeypatch):
    import sre_agent.war_room as war_room

    async def fake_decide(incident_id, gate, approved, approver_email):
        assert incident_id == "inc-1"
        assert gate == "start_fix"
        assert approved is True
        assert approver_email == "oncall@example.com"
        return {"mode": "gate_decision", "status": "ok", "message": "✅ Approved `start_fix` — thanks oncall@example.com."}

    monkeypatch.setattr(war_room, "_decide_gate_for_incident", fake_decide)

    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        posts = []

        async def poster(thread, text):
            posts.append(text)

        result = await route_gate_command("approve start-fix", t, reg, "oncall@example.com", poster)
        return result, posts

    result, posts = asyncio.run(scenario())
    assert result["status"] == "ok"
    assert posts == ["✅ Approved `start_fix` — thanks oncall@example.com."]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
