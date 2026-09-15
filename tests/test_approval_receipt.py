#!/usr/bin/env python3
"""An "approve fix" reply must be acknowledged when it lands, not when it ends.

Live on 2026-09-15, incident `3ed8be00` ([pdf-thumbnailer] PodOOMKilled). The
human typed `approve fix` at 01:31:23. Slack said nothing back until 01:34:27
— three minutes — and what it finally said was:

    ✅ Approved — remediation is running.

posted *underneath* the executor summary and the full resolution report for a
remediation that had already finished and verified. Both wrong at once: three
minutes of silence on the only channel this platform talks over, then a
present-tense claim about work that was already done.

The cause was ordering, not Slack: `route_fix_approval_command` awaited
`_decide_action_approval_for_incident`, which awaits `decide_action_approval`,
which resumes the graph and runs the remediation *and* its 180s verification
inline before returning the message to post.

Silence invites a re-send. The same human sent a duplicate `approve fix` on a
neighbouring thread 15 seconds later.

These tests hold the receipt to the moment the approval is durably committed —
`approval_flow.notify_authorized`, called straight after the CAS.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from sre_agent.approval_flow import notify_authorized
from sre_agent.war_room import ThreadRef, WarRoomRegistry, route_fix_approval_command

INCIDENT_ID = str(uuid.uuid4())
ORG_ID = str(uuid.uuid4())
CLUSTER_ID = str(uuid.uuid4())
APPROVER = "oncall@example.com"


# ── fakes for the DB work _decide_action_approval_for_incident does ──────────
class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Incident:
    cluster_id = CLUSTER_ID


class _Cluster:
    org_id = ORG_ID


class _User:
    def __init__(self, role):
        self.id = uuid.uuid4()
        self.org_id = ORG_ID
        self.email = APPROVER
        self.role = role


class _Session:
    def __init__(self, user):
        self._user = user

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _model, _key):
        return _Incident()

    async def execute(self, _stmt):
        return _Result(self._user)


def _install(monkeypatch, *, decide, user_role=None, pending_id="approval-1"):
    """Point the real handler at fakes, leaving its own logic intact."""
    from backend import crud, database, models

    from sre_agent import approval_flow

    role = models.UserRole.ADMIN if user_role is None else user_role
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _Session(_User(role)))

    async def _cluster(_db, _cluster_id):
        return _Cluster()

    monkeypatch.setattr(crud, "get_cluster_by_id", _cluster)

    async def _pending(*, incident_id):
        return pending_id

    monkeypatch.setattr(approval_flow, "find_latest_pending_action_approval", _pending)
    monkeypatch.setattr(approval_flow, "decide_action_approval", decide)


def _run(poster_impl=None):
    """Drive the real router and return (result, posts)."""
    posts: list = []

    async def poster(_thread, text):
        if poster_impl is not None:
            poster_impl(text)
        posts.append(text)

    async def scenario():
        registry = WarRoomRegistry()
        thread = ThreadRef("C1", "T1")
        registry.open(INCIDENT_ID, thread)
        result = await route_fix_approval_command(
            "approve fix", thread, registry, APPROVER, poster
        )
        return result, posts

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

def test_the_human_hears_back_before_the_remediation_runs(monkeypatch):
    """The ack must be posted from inside the approval, not after it."""
    timeline: list = []

    async def decide(*, on_authorized=None, **_kw):
        await on_authorized()
        timeline.append("remediation-started")
        # Stand in for the 3 minutes of execution + verification.
        await asyncio.sleep(0)
        timeline.append("remediation-finished")
        return "pending_acknowledgment"

    def record(text):
        timeline.append(f"posted:{text[:20]}")

    _install(monkeypatch, decide=decide)
    result, posts = _run(poster_impl=record)

    assert result["status"] == "ok"
    assert timeline[0].startswith("posted:")
    assert timeline.index("remediation-finished") > 0
    assert len(posts) == 1
    assert "remediation is running now" in posts[0]
    assert APPROVER in posts[0]


def test_a_finished_remediation_does_not_post_a_stale_running_notice(monkeypatch):
    """The exact `3ed8be00` symptom: the trailing message is suppressed once
    the receipt has gone out, because the run posts its own report."""

    async def decide(*, on_authorized=None, **_kw):
        await on_authorized()
        return "pending_acknowledgment"

    _install(monkeypatch, decide=decide)
    _result, posts = _run()

    assert len(posts) == 1, f"expected one message, got {posts}"


def test_a_resume_that_fails_after_authorization_is_reported_too(monkeypatch):
    """The receipt is already out and true — the approval committed. The
    failure that follows is new information and must still be posted."""

    async def decide(*, on_authorized=None, **_kw):
        await on_authorized()
        raise RuntimeError("graph resume exploded")

    _install(monkeypatch, decide=decide)
    result, posts = _run()

    assert result["status"] == "error"
    assert len(posts) == 2
    assert "remediation is running now" in posts[0]
    assert "failed to resume" in posts[1]


def test_a_receipt_that_never_reached_slack_does_not_silence_the_result(
    monkeypatch,
):
    """`notify_authorized` swallows a posting failure so Slack being down can
    never unwind a committed approval. That makes "we tried" ≠ "they know",
    so the final message has to come through instead of being suppressed."""
    attempts = {"n": 0}

    async def decide(*, on_authorized=None, **_kw):
        await on_authorized()  # notify_authorized would swallow the raise
        return "pending_acknowledgment"

    def explode_once(_text):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("slack 503")

    _install(monkeypatch, decide=decide)

    posts: list = []

    async def poster(_thread, text):
        explode_once(text)
        posts.append(text)

    async def scenario():
        registry = WarRoomRegistry()
        thread = ThreadRef("C1", "T1")
        registry.open(INCIDENT_ID, thread)

        # The real swallow, wrapping the real callback.
        async def decide_with_swallow(*, on_authorized=None, **_kw):
            await notify_authorized(on_authorized, "approval-1")
            return "pending_acknowledgment"

        from sre_agent import approval_flow

        monkeypatch.setattr(
            approval_flow, "decide_action_approval", decide_with_swallow
        )
        return await route_fix_approval_command(
            "approve fix", thread, registry, APPROVER, poster
        )

    result = asyncio.run(scenario())

    assert result["status"] == "ok"
    # First post raised, so only the fallback landed — the human is not left
    # with nothing.
    assert len(posts) == 1
    assert "Approved" in posts[0]


# ---------------------------------------------------------------------------
# No receipt without an approval
# ---------------------------------------------------------------------------

def test_a_non_admin_never_sees_an_approval_receipt(monkeypatch):
    """The receipt claims the approval is committed. Refusals happen before
    the CAS, so the callback must never fire for one."""
    from backend import models

    async def decide(**_kw):  # pragma: no cover - must not be reached
        raise AssertionError("a non-admin reached the approval")

    _install(monkeypatch, decide=decide, user_role=models.UserRole.MEMBER)
    result, posts = _run()

    assert result["status"] == "denied"
    assert len(posts) == 1
    assert "Only admins" in posts[0]
    assert "running now" not in posts[0]


def test_no_pending_approval_posts_only_the_explanation(monkeypatch):
    async def decide(**_kw):  # pragma: no cover - must not be reached
        raise AssertionError("decided a non-existent approval")

    _install(monkeypatch, decide=decide, pending_id=None)
    result, posts = _run()

    assert result["status"] == "not_found"
    assert len(posts) == 1
    assert "No pending remediation approval" in posts[0]


# ---------------------------------------------------------------------------
# notify_authorized itself
# ---------------------------------------------------------------------------

def test_notify_authorized_reports_a_delivered_notification():
    calls: list = []

    async def cb():
        calls.append(1)

    assert asyncio.run(notify_authorized(cb, "approval-1")) is True
    assert calls == [1]


def test_notify_authorized_swallows_a_failure_so_the_approval_stands():
    async def cb():
        raise RuntimeError("slack is down")

    # No raise: the CAS has already committed APPROVED by this point.
    assert asyncio.run(notify_authorized(cb, "approval-1")) is False


def test_notify_authorized_without_a_callback_is_a_no_op():
    assert asyncio.run(notify_authorized(None, "approval-1")) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
