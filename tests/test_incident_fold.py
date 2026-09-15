#!/usr/bin/env python3
"""One fault on one service must not become six Slack threads.

Exact-title dedup gives one war room per *title*. A pod that runs out of
memory produces two titles — `[pdf-thumbnailer] PodOOMKilled` and
`[pdf-thumbnailer] PodCrashLooping` — so it produced two incidents, two war
rooms, two investigations and two remediations, for one pod with one wrong
memory limit. Live on 2026-09-15 that ran to six pdf-thumbnailer threads in
75 minutes, and the report back was:

    "there are too many incidents and i cannot find them correctly in slack"

The correlation gate watched the whole thing from shadow mode
(`_record_correlation_shadow`, Phase A) and scored the duplicate
`88fa9ee4` ↔ `3ed8be00` at **1.00** without being allowed to do anything
about it.

Eighteen shadow verdicts later, that record says which half of the signal is
safe to act on:

* same service, 12 verdicts, 12 correct;
* cross-service on text similarity alone, 6 verdicts, **3 wrong** — a generic
  Kubernetes alert renders identical boilerplate whatever service it fires
  on, so `[thumb-worker] PodCrashLooping` scored 0.62 against
  `[ocr-extractor] PodCrashLooping`. Three different services, three memory
  limits, three fixes.

So the gate acts on same-service and keeps shadowing the rest. These tests
hold that line, and hold the two conditions that make a fold safe: something
must actually be working the parent, and the fold must be visible in Slack.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import crud, models
from sre_agent import job_store, job_worker, war_room_service
from sre_agent.api.v1 import alerts as alerts_module
from sre_agent.incident_correlation import CorrelationCandidate, actionable_bundle

NOW = datetime(2026, 9, 15, 1, 17, 0, tzinfo=timezone.utc)
# Inside `DEFAULT_WINDOW_MINUTES`, so proximity is never what a test measures.
PARENT_CREATED = NOW - timedelta(minutes=14)

SERVICE = "pdf-thumbnailer"
PARENT_TITLE = f"[{SERVICE}] PodOOMKilled"
FOLDED_TITLE = f"[{SERVICE}] PodCrashLooping"


def _candidate(title, incident_id="cand", created_at=NOW, description=""):
    return CorrelationCandidate(
        incident_id=incident_id,
        cluster_id="c1",
        title=title,
        description=description,
        created_at=created_at,
    )


def _incident(
    status=models.IncidentStatus.AWAITING_APPROVAL,
    title=PARENT_TITLE,
    created_at=PARENT_CREATED,
    *,
    thread=True,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        title=title,
        description="pod was OOMKilled, memory limit 64Mi",
        status=status,
        created_at=created_at,
        slack_channel="C0C0KA474LC" if thread else None,
        slack_thread_ts="1789434811.220989" if thread else None,
    )


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def no_live_job(monkeypatch):
    """Default: the job table says nothing is running.

    Only `open` and `investigating` consult it, and the parent in most tests
    is `awaiting_approval` — active, not parked, because a human is already
    holding a question about it.
    """
    async def none_running(_db, _incident_id):
        return False

    monkeypatch.setattr(job_store, "has_live_investigation_job", none_running)


@pytest.fixture
def spies(monkeypatch):
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


def _open_pool(monkeypatch, incidents):
    async def listing(_db, _cluster_id, exclude_incident_id=None):
        return list(incidents)

    monkeypatch.setattr(crud, "list_active_incidents_for_cluster", listing)


def _find(monkeypatch, incidents, title=FOLDED_TITLE, description="pod crashlooping"):
    _open_pool(monkeypatch, incidents)
    cluster = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4())
    return asyncio.run(
        alerts_module._find_fold_target(
            FakeSession(), cluster, title, description, now=NOW
        )
    )


# ---------------------------------------------------------------------------
# What the gate is allowed to act on
# ---------------------------------------------------------------------------

def test_the_same_service_bundle_shadow_mode_validated_is_actionable():
    """`[pdf-thumbnailer] PodCrashLooping` ↔ `[pdf-thumbnailer] PodOOMKilled`,
    the live `88fa9ee4` ↔ `3ed8be00` pair that scored 1.00."""
    parent = _candidate(PARENT_TITLE, incident_id="parent", created_at=PARENT_CREATED)
    match = actionable_bundle(_candidate(FOLDED_TITLE), [parent])

    assert match is not None
    assert match.incident_id == "parent"
    assert match.score == pytest.approx(1.0)


def test_two_services_that_only_share_alert_boilerplate_are_not_actionable():
    """The three shadow verdicts that were wrong. `correlate` still reports
    the correlation — it is real text similarity — but a shared alert template
    is not evidence that two services share a fault."""
    other = _candidate(
        "[ocr-extractor] PodCrashLooping",
        incident_id="other",
        created_at=PARENT_CREATED,
        description="Pod is restarting repeatedly (CrashLoopBackOff)",
    )
    candidate = _candidate(
        "[thumb-worker] PodCrashLooping",
        description="Pod is restarting repeatedly (CrashLoopBackOff)",
    )

    from sre_agent.incident_correlation import correlate

    # The correlation is genuinely scored — nothing about the shadow record
    # changes — it is only the licence to act that is withheld.
    assert correlate(candidate, [other]).decision == "bundle"
    assert actionable_bundle(candidate, [other]) is None


def test_an_adjacent_service_is_not_the_same_service():
    """Topology adjacency scores 0.6 and can outrank the threshold, but
    `[checkout-service] PaymentFailureSpike` is not `[payment-service]
    PaymentProviderDown`, and one fix does not cover both."""
    other = _candidate(
        "[payment-service] PaymentProviderDown",
        incident_id="other",
        created_at=PARENT_CREATED,
    )
    candidate = _candidate("[checkout-service] PaymentFailureSpike")
    adjacency = {"checkout-service": ["payment-service"]}

    assert actionable_bundle(candidate, [other], adjacency=adjacency) is None


def test_the_same_service_wins_over_a_higher_scoring_stranger():
    """The same-service match is the one that gets picked out of the list,
    not merely the top of it."""
    stranger = _candidate(
        "[thumb-worker] PodCrashLooping",
        incident_id="stranger",
        created_at=PARENT_CREATED,
        description="Pod is restarting repeatedly (CrashLoopBackOff)",
    )
    sibling = _candidate(
        PARENT_TITLE, incident_id="sibling", created_at=PARENT_CREATED
    )
    candidate = _candidate(
        FOLDED_TITLE, description="Pod is restarting repeatedly (CrashLoopBackOff)"
    )

    match = actionable_bundle(candidate, [stranger, sibling])
    assert match is not None
    assert match.incident_id == "sibling"


def test_a_title_without_a_service_prefix_never_folds():
    """`extract_service` falls back to the whole title, which would make two
    prefix-less incidents look like one service."""
    assert actionable_bundle(_candidate(""), [_candidate("x", "o")]) is None


# ---------------------------------------------------------------------------
# When a fold target is chosen
# ---------------------------------------------------------------------------

def test_an_actively_worked_sibling_is_the_fold_target(monkeypatch):
    parent = _incident()
    assert _find(monkeypatch, [parent]) is parent


def test_nothing_open_means_nothing_to_fold_into(monkeypatch):
    assert _find(monkeypatch, []) is None


@pytest.mark.parametrize(
    "status",
    [
        models.IncidentStatus.INVESTIGATED,
        models.IncidentStatus.REMEDIATION_FAILED,
        models.IncidentStatus.VERIFICATION_UNKNOWN,
        models.IncidentStatus.PENDING_ACKNOWLEDGMENT,
    ],
)
def test_a_parked_sibling_is_never_folded_into(monkeypatch, status):
    """Nothing is working the parent, so folding would mean nothing works the
    folded alert either — and unlike exact-title dedup, there is a choice
    here. The choice is a real incident with a real investigation."""
    assert _find(monkeypatch, [_incident(status)]) is None


def test_a_sibling_with_no_slack_thread_is_never_folded_into(monkeypatch):
    """Slack is the only channel this platform has. A fold with nowhere to
    announce itself is an alert that silently disappears."""
    assert _find(monkeypatch, [_incident(thread=False)]) is None


def test_a_sibling_older_than_the_scoring_window_still_folds(monkeypatch):
    """`DEFAULT_WINDOW_MINUTES` is 15 and the live pair cleared it by a hair
    (01:02 → 01:17). A remediation with human approval and a 180s
    verification routinely runs longer, and the fold pool is already limited
    to incidents something is actively working — so the fold uses its own,
    wider window rather than letting the clock alone reintroduce the
    duplicate threads."""
    parent = _incident(created_at=NOW - timedelta(minutes=45))
    assert _find(monkeypatch, [parent]) is parent


def test_a_sibling_older_than_the_fold_window_does_not_fold(monkeypatch):
    """Wider is not unbounded."""
    older = timedelta(minutes=alerts_module._FOLD_WINDOW_MINUTES + 5)
    assert _find(monkeypatch, [_incident(created_at=NOW - older)]) is None


def test_a_different_service_is_not_a_fold_target(monkeypatch):
    parent = _incident(title="[ocr-extractor] PodCrashLooping")
    assert _find(monkeypatch, [parent], title="[thumb-worker] PodCrashLooping") is None


# ---------------------------------------------------------------------------
# Performing the fold
# ---------------------------------------------------------------------------

def _fold(db, parent, spies_alert=None):
    alert = spies_alert or {
        "alertname": "PodCrashLooping",
        "labels": {"alertname": "PodCrashLooping", "service": SERVICE},
    }
    return asyncio.run(
        alerts_module._fold_alert_into_incident(db, parent, alert, FOLDED_TITLE, SERVICE)
    )


def test_a_fold_tells_the_parent_thread_and_records_it(spies):
    parent = _incident()
    assert _fold(FakeSession(), parent) is True

    assert len(spies.posts) == 1
    posted_id, message = spies.posts[0]
    assert posted_id == str(parent.id)
    assert FOLDED_TITLE in message
    assert SERVICE in message
    # The part that must be said out loud: nothing separate will be done.
    assert "no separate investigation will run" in message
    # And the way out, if the fold was wrong.
    assert "mark resolved" in message

    assert len(spies.events) == 1
    assert spies.events[0]["event_type"] == alerts_module._FOLD_EVENT_TYPE
    assert spies.events[0]["payload"]["folded_title"] == FOLDED_TITLE


def test_a_fold_nobody_can_see_is_not_a_fold(monkeypatch):
    """Write-then-notify is inverted on purpose. A fold the on-call cannot
    read is worse than the duplicate thread it saves them, so an undelivered
    notice means the alert gets its own incident after all."""
    async def undelivered(_incident_id, _message):
        return False

    events = []

    async def fake_event(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", undelivered)
    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_event)

    assert _fold(FakeSession(), _incident()) is False
    assert events == [], "nothing may be recorded for a fold that did not happen"


def test_slack_blowing_up_never_reaches_the_webhook(monkeypatch):
    """A 500 makes Alertmanager retry the whole group. Losing the fold is
    fine; losing the alert is not."""
    async def boom(_incident_id, _message):
        raise RuntimeError("slack is on fire")

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", boom)
    assert _fold(FakeSession(), _incident()) is False


def test_a_failed_audit_row_does_not_unfold_an_announced_fold(monkeypatch):
    """The thread has already been told. Opening an incident now would
    contradict a message the on-call can read."""
    posts = []

    async def fake_post(incident_id, message):
        posts.append(message)
        return True

    async def explode(*_args, **_kwargs):
        raise RuntimeError("timeline write failed")

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", fake_post)
    monkeypatch.setattr(crud, "create_incident_timeline_event", explode)

    db = FakeSession()
    assert _fold(db, _incident()) is True
    assert len(posts) == 1
    assert db.rollbacks == 1


# ---------------------------------------------------------------------------
# Wired into the endpoint
# ---------------------------------------------------------------------------

def _live_incident(**kwargs):
    """A parent inside the correlation window of *real* now.

    The endpoint stamps its own `now` (`datetime.now`), so an endpoint test
    cannot pin the clock the way the unit tests above do — the parent has to
    be genuinely recent instead.
    """
    kwargs.setdefault("created_at", datetime.now(timezone.utc) - timedelta(minutes=1))
    return _incident(**kwargs)


class _Request:
    def __init__(self, alertname, service=SERVICE):
        self._alertname = alertname
        self._service = service

    async def json(self):
        return {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": self._alertname,
                        "service": self._service,
                        "severity": "warning",
                    },
                    "annotations": {
                        "summary": "pod restarting",
                        "description": "CrashLoopBackOff",
                    },
                    "startsAt": "2026-09-15T01:17:00Z",
                    "endsAt": "",
                }
            ],
        }


@pytest.fixture
def endpoint(monkeypatch):
    """Everything the webhook touches except the fold path itself."""
    created = []

    async def noop(*_args, **_kwargs):
        return None

    async def no_duplicate(*_args, **_kwargs):
        return None

    async def create_incident(_db, incident_data, _cluster_id):
        created.append(incident_data.title)
        return _incident(models.IncidentStatus.OPEN, title=incident_data.title)

    async def enqueue(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crud, "update_cluster_heartbeat", noop)
    monkeypatch.setattr(crud, "lock_incident_dedup", noop)
    monkeypatch.setattr(crud, "find_duplicate_incident", no_duplicate)
    monkeypatch.setattr(crud, "create_incident", create_incident)
    monkeypatch.setattr(alerts_module, "_record_correlation_shadow", noop)
    monkeypatch.setattr(job_worker, "enqueue_and_kick", enqueue)
    return SimpleNamespace(created=created)


def _webhook(db, alertname="PodCrashLooping", service=SERVICE):
    cluster = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4())
    return asyncio.run(
        alerts_module.receive_alertmanager_webhook(_Request(alertname, service), cluster, db)
    )


def test_the_webhook_folds_instead_of_opening_a_second_war_room(
    endpoint, spies, monkeypatch
):
    """The live `88fa9ee4` path, end to end: `PodCrashLooping` arrives while
    `PodOOMKilled` is open on the same service, and no second thread appears."""
    parent = _live_incident()
    _open_pool(monkeypatch, [parent])

    result = _webhook(FakeSession())

    assert result["incidents_created"] == 0
    assert result["incidents_folded"] == 1
    assert endpoint.created == []
    assert len(spies.posts) == 1
    assert spies.posts[0][0] == str(parent.id)
    assert result["reconciliations"][0]["reason"] == "folded_into_correlated_incident"


def test_an_unrelated_service_still_opens_its_own_incident(
    endpoint, spies, monkeypatch
):
    _open_pool(monkeypatch, [_live_incident()])

    result = _webhook(FakeSession(), service="checkout-service")

    assert result["incidents_created"] == 1
    assert result["incidents_folded"] == 0
    assert endpoint.created == ["[checkout-service] PodCrashLooping"]
    assert spies.posts == []


def test_an_undelivered_fold_notice_falls_through_to_a_real_incident(
    endpoint, monkeypatch
):
    """The safety valve, wired: Slack refuses the notice, so the alert gets
    the incident it would have had."""
    async def undelivered(_incident_id, _message):
        return False

    monkeypatch.setattr(war_room_service, "post_to_incident_thread", undelivered)
    monkeypatch.setattr(crud, "create_incident_timeline_event", lambda *a, **k: None)
    _open_pool(monkeypatch, [_live_incident()])

    result = _webhook(FakeSession())

    assert result["incidents_folded"] == 0
    assert result["incidents_created"] == 1
    assert endpoint.created == [FOLDED_TITLE]


def test_a_broken_fold_lookup_never_reaches_the_webhook(endpoint, monkeypatch):
    """Fail open. A 500 makes Alertmanager retry the whole group ten times,
    and an extra thread is a far smaller loss than an alert that never
    arrives."""
    async def explode(*_args, **_kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(crud, "list_active_incidents_for_cluster", explode)

    result = _webhook(FakeSession())

    assert result["incidents_created"] == 1
    assert result["incidents_folded"] == 0


def test_the_dedup_lock_is_retaken_before_creating_after_a_failed_fold(
    endpoint, monkeypatch
):
    """The fold path commits to release the dedup advisory lock before its
    Slack round trip. Falling through to `create_incident` without retaking it
    would reopen the check-then-create race `lock_incident_dedup` closes."""
    locks = []

    async def record_lock(_db, _cluster_id, title):
        locks.append(title)

    async def undelivered(_incident_id, _message):
        return False

    monkeypatch.setattr(crud, "lock_incident_dedup", record_lock)
    monkeypatch.setattr(war_room_service, "post_to_incident_thread", undelivered)
    _open_pool(monkeypatch, [_live_incident()])

    _webhook(FakeSession())

    assert locks == [FOLDED_TITLE, FOLDED_TITLE]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
