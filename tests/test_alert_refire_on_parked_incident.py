#!/usr/bin/env python3
"""A re-firing alert must not vanish into an incident nobody is working on.

Dedup collapses a re-firing alert into the incident already tracking it for as
long as that incident is non-resolved. That is right for one war room per
condition, but "non-resolved" hides two different situations. In OPEN,
INVESTIGATING and REMEDIATION_IN_PROGRESS something is running; in
AWAITING_APPROVAL a question is already in front of a human. In INVESTIGATED,
REMEDIATION_FAILED, VERIFICATION_UNKNOWN and PENDING_ACKNOWLEDGMENT nothing is
running and nobody has been asked anything — no future event will move the
incident, so the alert is swallowed and will be swallowed again every group
interval.

Live on 2026-09-14T11:08:00Z. Incident `d3ca5138` investigated a checkout
memory leak, requested approval, got no reply; the lapse sweep retired the
request, moved it to `investigated`, and told the thread "the problem is still
open ... to act on it now, a human has to take it from here or re-run the
investigation to raise a fresh approval". The leak was then driven back over
the threshold, `CheckoutMemoryApproachingLimit` fired again, and the webhook
logged `Dedup: ... already open as incident d3ca5138` and dropped it. No
timeline event, no Slack message. Slack is the only channel this product has.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import crud, models
from sre_agent import job_worker, war_room, war_room_service
from sre_agent.api.v1 import alerts as alerts_module

NOW = datetime(2026, 9, 14, 11, 8, 0, tzinfo=timezone.utc)


def _alert(alertname="CheckoutMemoryApproachingLimit", service="checkout-service"):
    return {
        "status": "firing",
        "alertname": alertname,
        "service": service,
        "severity": "warning",
        "summary": "checkout memory high",
        "description": "above 200MB (pod limit is 256Mi)",
        "labels": {"alertname": alertname, "service": service},
        "startsAt": "2026-09-14T11:07:00Z",
        "endsAt": "",
    }


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class FakeSession:
    """Answers exactly one query: the newest re-fire event for an incident."""

    def __init__(self, last_refire=None, *, explode=False):
        self.last_refire = last_refire
        self.explode = explode
        self.commits = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, params=None):
        if self.explode:
            raise RuntimeError("database is down")
        return FakeResult(self.last_refire)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("the webhook must not roll back")


@pytest.fixture
def spies(monkeypatch):
    """Capture what the notice writes and what it says."""
    events = []
    posts = []

    async def fake_event(_db, incident_id, **kwargs):
        events.append({"incident_id": incident_id, **kwargs})
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_post(incident_id, message):
        posts.append((incident_id, message))
        return True

    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_event)
    monkeypatch.setattr(war_room_service, "post_to_incident_thread", fake_post)
    return SimpleNamespace(events=events, posts=posts)


def _incident(status, title="[checkout-service] CheckoutMemoryApproachingLimit"):
    return SimpleNamespace(id=uuid.uuid4(), title=title, status=status)


def _announce(db, incident, alert=None, now=NOW):
    return asyncio.run(
        alerts_module._announce_refire_on_parked_incident(
            db, incident, alert or _alert(), now=now
        )
    )


# ---------------------------------------------------------------------------
# Which statuses speak
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    [
        models.IncidentStatus.INVESTIGATED,
        models.IncidentStatus.REMEDIATION_FAILED,
        models.IncidentStatus.VERIFICATION_UNKNOWN,
        models.IncidentStatus.PENDING_ACKNOWLEDGMENT,
    ],
)
def test_a_parked_incident_is_told_its_alert_came_back(spies, status):
    incident = _incident(status)

    assert _announce(FakeSession(), incident) is True

    assert len(spies.posts) == 1
    posted_id, message = spies.posts[0]
    assert posted_id == str(incident.id)
    assert "CheckoutMemoryApproachingLimit" in message
    assert status.value in message
    # The title is "[service] AlertName", so naming the alert separately would
    # print it twice — the stutter the first live notice had.
    assert message.count("CheckoutMemoryApproachingLimit") == 1
    assert len(spies.events) == 1
    assert spies.events[0]["event_type"] == "alert_refired"


@pytest.mark.parametrize(
    "status",
    [
        models.IncidentStatus.OPEN,
        models.IncidentStatus.INVESTIGATING,
        models.IncidentStatus.AWAITING_APPROVAL,
        models.IncidentStatus.REMEDIATION_IN_PROGRESS,
    ],
)
def test_an_incident_that_is_being_worked_is_left_alone(spies, status):
    """Something is running, or a human already has the question. Repeating
    ourselves every group interval would be noise, and noise on the only
    channel is how the real notices get ignored."""
    assert _announce(FakeSession(), _incident(status)) is False
    assert spies.posts == []
    assert spies.events == []


def test_every_parked_status_is_a_non_working_status():
    """Guards the split itself: if a new status is added to the active set it
    must be classified deliberately, not inherited by accident."""
    working = {
        models.IncidentStatus.OPEN,
        models.IncidentStatus.INVESTIGATING,
        models.IncidentStatus.AWAITING_APPROVAL,
        models.IncidentStatus.REMEDIATION_IN_PROGRESS,
    }
    assert alerts_module._PARKED_INCIDENT_STATUSES | working == set(
        crud._ACTIVE_INCIDENT_STATUSES
    )
    assert not (alerts_module._PARKED_INCIDENT_STATUSES & working)


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------

def test_the_notice_says_what_did_not_happen_not_just_the_status_name(spies):
    """`investigated` reads like success to anyone who does not know the enum.
    The thread has to say the cluster was never touched."""
    _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED))
    message = spies.posts[0][1]
    assert "nothing was changed on the cluster" in message


def test_a_verified_fix_coming_back_is_reported_as_a_regression(spies):
    _announce(FakeSession(), _incident(models.IncidentStatus.PENDING_ACKNOWLEDGMENT))
    message = spies.posts[0][1]
    assert "did not hold" in message


def test_the_notice_says_no_one_else_will_pick_this_up(spies):
    """The dead end is the news. Without it the reader assumes the silence
    means something is in flight."""
    _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED))
    message = spies.posts[0][1]
    assert "will not open a second incident" in message
    assert "will not re-investigate on its own" in message


def test_the_only_way_out_named_is_one_the_thread_actually_accepts(spies):
    """`mark resolved` is a real war-room command; "re-run the investigation"
    is not one, and POST /incidents/trigger dedups on the same title, so
    offering it would send the on-call looking for a button that is not
    there."""
    _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED))
    message = spies.posts[0][1]
    assert "mark resolved" in message
    assert war_room.is_resolve_command("mark resolved")


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def test_the_thread_is_not_told_again_inside_the_cooldown(spies):
    """Alertmanager redelivers the firing group about once a minute."""
    db = FakeSession(last_refire=NOW - timedelta(minutes=5))
    assert _announce(db, _incident(models.IncidentStatus.INVESTIGATED)) is False
    assert spies.posts == []
    assert spies.events == []


def test_the_notice_returns_once_the_cooldown_has_passed(spies):
    """Still firing an hour later, still unattended — worth saying again."""
    db = FakeSession(
        last_refire=NOW
        - timedelta(minutes=alerts_module._REFIRE_NOTICE_COOLDOWN_MINUTES + 1)
    )
    assert _announce(db, _incident(models.IncidentStatus.INVESTIGATED)) is True
    assert len(spies.posts) == 1


def test_a_naive_timestamp_from_the_database_does_not_crash_the_cooldown(spies):
    """`created_at` comes back without a tzinfo on some drivers; comparing it
    to an aware `now` raises, and the except would swallow the notice."""
    db = FakeSession(last_refire=(NOW - timedelta(minutes=5)).replace(tzinfo=None))
    assert _announce(db, _incident(models.IncidentStatus.INVESTIGATED)) is False
    assert spies.posts == []


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_the_cooldown_is_recorded_before_slack_is_attempted(spies, monkeypatch):
    """If the post were recorded only on success, a Slack outage would turn
    every group interval into another attempt."""
    order = []

    async def fake_event(_db, incident_id, **kwargs):
        order.append("timeline")
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_post(incident_id, message):
        order.append("slack")
        return False

    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_event)
    monkeypatch.setattr(war_room_service, "post_to_incident_thread", fake_post)

    assert _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED)) is False
    assert order == ["timeline", "slack"]


def test_an_undelivered_notice_is_logged_as_an_error(spies, monkeypatch, caplog):
    async def fake_post(incident_id, message):
        return False

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", fake_post)
    with caplog.at_level(logging.ERROR, logger=alerts_module.logger.name):
        _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED))
    assert "NO Slack notice was delivered" in caplog.text


def test_slack_blowing_up_never_reaches_the_webhook(spies, monkeypatch):
    """A 500 makes Alertmanager retry the whole group ten times. Losing the
    notice is bad; losing the alert is worse."""
    async def boom(incident_id, message):
        raise RuntimeError("slack is on fire")

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", boom)
    assert _announce(FakeSession(), _incident(models.IncidentStatus.INVESTIGATED)) is False


def test_a_failed_cooldown_lookup_never_reaches_the_webhook(spies):
    db = FakeSession(explode=True)
    assert _announce(db, _incident(models.IncidentStatus.INVESTIGATED)) is False
    assert spies.posts == []


# ---------------------------------------------------------------------------
# Wired into the endpoint
# ---------------------------------------------------------------------------

def test_the_dedup_branch_posts_the_notice_and_still_dedups(monkeypatch, spies):
    """The whole defect was in the dedup branch: it must keep collapsing the
    alert into the one incident *and* say so."""
    db = FakeSession()
    cluster = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4())
    parked = _incident(models.IncidentStatus.INVESTIGATED)
    created = []

    async def noop(*args, **kwargs):
        return None

    async def find_duplicate(_db, _cluster_id, title, window_minutes=None):
        return parked if title == parked.title else None

    async def create_incident(_db, incident_data, _cluster_id):
        created.append(incident_data.title)
        return _incident(models.IncidentStatus.OPEN, title=incident_data.title)

    async def enqueue(**kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crud, "update_cluster_heartbeat", noop)
    monkeypatch.setattr(crud, "lock_incident_dedup", noop)
    monkeypatch.setattr(crud, "find_duplicate_incident", find_duplicate)
    monkeypatch.setattr(crud, "create_incident", create_incident)
    monkeypatch.setattr(alerts_module, "_record_correlation_shadow", noop)
    monkeypatch.setattr(job_worker, "enqueue_and_kick", enqueue)

    class FakeRequest:
        async def json(self):
            return {
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "CheckoutMemoryApproachingLimit",
                            "service": "checkout-service",
                            "severity": "warning",
                        },
                        "annotations": {"summary": "s", "description": "d"},
                        "startsAt": "2026-09-14T11:07:00Z",
                        "endsAt": "",
                    }
                ],
            }

    result = asyncio.run(
        alerts_module.receive_alertmanager_webhook(FakeRequest(), cluster, db)
    )

    assert result["incidents_created"] == 0
    assert created == []
    assert len(spies.posts) == 1
    assert spies.posts[0][0] == str(parked.id)
