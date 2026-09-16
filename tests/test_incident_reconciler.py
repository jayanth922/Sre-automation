#!/usr/bin/env python3
"""An approved remediation that dies with its process must not be silent.

`decide_action_approval` consumes the approval and then drives the remediation
synchronously in the caller's process. There is no durable job, no lease and no
retry, so a restart in the middle leaves the incident in the transient
`REMEDIATION_IN_PROGRESS` with the approval already spent — permanently, and
with the last Slack message still reading "remediation is running".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.incident_reconciler import (
    interrupted_message,
    is_interrupted,
    last_activity_at,
    stale_after,
    sweep_interval,
)

UTC = timezone.utc


def _at(minute: int) -> datetime:
    return datetime(2026, 9, 14, 6, minute, tzinfo=UTC)


def test_progress_is_reconstructed_from_whichever_row_moved_last():
    """`incidents` has no `updated_at`, so silence is measured against every
    row a live run writes — the newest of them wins."""
    assert last_activity_at(
        created_at=_at(0), timeline_at=_at(30), decided_at=_at(10)
    ) == _at(30)
    assert last_activity_at(
        created_at=_at(0), timeline_at=_at(5), decided_at=_at(40)
    ) == _at(40)
    assert last_activity_at(
        created_at=_at(0), timeline_at=None, decided_at=None, manifest_at=_at(50)
    ) == _at(50)


def test_a_run_with_nothing_but_a_creation_time_falls_back_to_it():
    assert last_activity_at(created_at=_at(12)) == _at(12)


def test_naive_timestamps_are_not_treated_as_the_distant_past():
    """A driver returning naive datetimes must not make every incident look
    stale (or blow up comparing naive to aware)."""
    naive = datetime(2026, 9, 14, 6, 30)
    assert last_activity_at(created_at=naive) == _at(30)


def test_a_slow_but_live_remediation_is_left_alone():
    """Live verification polls the alert for minutes; the default threshold has
    to sit well clear of that or the sweep would kill healthy runs."""
    assert stale_after() >= timedelta(minutes=10)
    assert not is_interrupted(
        last_activity=_at(30), now=_at(35), threshold=timedelta(minutes=20)
    )


def test_a_remediation_that_stopped_reporting_is_interrupted():
    assert is_interrupted(
        last_activity=_at(0), now=_at(45), threshold=timedelta(minutes=20)
    )


def test_the_threshold_is_exclusive_at_the_boundary():
    assert not is_interrupted(
        last_activity=_at(0), now=_at(20), threshold=timedelta(minutes=20)
    )
    assert is_interrupted(
        last_activity=_at(0), now=_at(21), threshold=timedelta(minutes=20)
    )


def test_the_sweep_interval_cannot_be_configured_into_a_hot_loop(monkeypatch):
    monkeypatch.setenv("INCIDENT_RECONCILE_SECONDS", "0")
    assert sweep_interval() >= 30.0
    monkeypatch.setenv("INCIDENT_RECONCILE_SECONDS", "not-a-number")
    assert sweep_interval() > 0


def test_the_stale_threshold_cannot_be_configured_to_zero(monkeypatch):
    monkeypatch.setenv("INCIDENT_STALE_REMEDIATION_MINUTES", "0")
    assert stale_after() >= timedelta(minutes=1)


def test_the_slack_notice_says_what_is_and_is_not_known():
    """The on-call must not read this as either 'fixed' or 'nothing happened'."""
    text = interrupted_message(title="[checkout-service] CheckoutHighErrorRate", silent_for=timedelta(minutes=182))
    assert "Remediation interrupted" in text
    assert "182 minute(s) ago" in text
    assert "not* known to be fixed" in text
    assert "verification_unknown" in text
    assert "needs a human" in text
    assert "resolved" not in text.lower().replace("unresolved", "")


@pytest.mark.asyncio
async def test_a_stranded_incident_is_moved_to_verification_unknown_and_announced(
    monkeypatch,
):
    """End-to-end over a fake session: scan, CAS the status, write the
    timeline, tell Slack."""
    import sre_agent.incident_reconciler as reconciler

    posted: list[tuple[str, str]] = []
    timeline: list[dict] = []

    class FakeIncident:
        def __init__(self):
            self.id = "c9e6fc3d-0000-4000-8000-000000000000"
            self.title = "[checkout-service] CheckoutHighErrorRate"
            self.created_at = _at(0)

    incident = FakeIncident()

    class FakeResult:
        def __init__(self, value=None, rowcount=0, scalars=None):
            self._value = value
            self.rowcount = rowcount
            self._scalars = scalars

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            outer = self

            class _S:
                def all(self_inner):
                    return outer._scalars

            return _S()

    class FakeDB:
        def __init__(self):
            self.calls = 0
            self.committed = 0

        async def execute(self, stmt):
            self.calls += 1
            text = str(stmt).lower()
            if text.startswith("update"):
                return FakeResult(rowcount=1)
            if "max(" in text:
                return FakeResult(value=None)
            return FakeResult(scalars=[incident])

        async def commit(self):
            self.committed += 1

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    db = FakeDB()
    monkeypatch.setattr(reconciler, "utc_now", lambda: _at(50))

    import backend.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: db)

    import backend.crud as crud

    async def fake_timeline(_db, incident_id, **kwargs):
        timeline.append({"incident_id": incident_id, **kwargs})

    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_timeline)

    import sre_agent.war_room_service as wrs

    async def fake_post(incident_id, text):
        posted.append((incident_id, text))
        return True

    monkeypatch.setattr(wrs, "post_to_incident_thread", fake_post)

    recovered = await reconciler.reconcile_interrupted_remediations(
        threshold=timedelta(minutes=20)
    )

    assert len(recovered) == 1
    assert recovered[0].new_status == "verification_unknown"
    assert recovered[0].slack_notified is True
    assert recovered[0].silent_for == timedelta(minutes=50)
    assert db.committed >= 1
    assert timeline and timeline[0]["event_type"] == "remediation_interrupted"
    assert posted and "Remediation interrupted" in posted[0][1]


@pytest.mark.asyncio
async def test_only_one_replica_claims_a_stranded_incident(monkeypatch):
    """The status write is a compare-and-set, so a replica that loses the race
    must neither post to Slack nor report a recovery."""
    import sre_agent.incident_reconciler as reconciler

    posted: list[str] = []

    class FakeIncident:
        id = "c9e6fc3d-0000-4000-8000-000000000000"
        title = "[checkout-service] CheckoutHighErrorRate"
        created_at = _at(0)

    class FakeResult:
        def __init__(self, value=None, rowcount=0, scalars=None):
            self._value = value
            self.rowcount = rowcount
            self._scalars = scalars

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            outer = self

            class _S:
                def all(self_inner):
                    return outer._scalars

            return _S()

    class FakeDB:
        async def execute(self, stmt):
            text = str(stmt).lower()
            if text.startswith("update"):
                return FakeResult(rowcount=0)  # another replica won
            if "max(" in text:
                return FakeResult(value=None)
            return FakeResult(scalars=[FakeIncident()])

        async def commit(self):
            raise AssertionError("must not commit after losing the CAS")

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(reconciler, "utc_now", lambda: _at(50))
    import backend.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: FakeDB())

    import sre_agent.war_room_service as wrs

    async def fake_post(incident_id, text):
        posted.append(incident_id)
        return True

    monkeypatch.setattr(wrs, "post_to_incident_thread", fake_post)

    recovered = await reconciler.reconcile_interrupted_remediations(
        threshold=timedelta(minutes=20)
    )
    assert recovered == []
    assert posted == []


@pytest.mark.asyncio
async def test_a_recovery_slack_never_received_is_logged_as_an_error(
    monkeypatch, caplog
):
    """Slack is the only channel this platform has; a silent recovery is only
    half a recovery and must be loud in the logs."""
    import logging

    import sre_agent.incident_reconciler as reconciler

    class FakeIncident:
        id = "c9e6fc3d-0000-4000-8000-000000000000"
        title = "[checkout-service] CheckoutHighErrorRate"
        created_at = _at(0)

    class FakeResult:
        def __init__(self, value=None, rowcount=0, scalars=None):
            self._value = value
            self.rowcount = rowcount
            self._scalars = scalars

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            outer = self

            class _S:
                def all(self_inner):
                    return outer._scalars

            return _S()

    class FakeDB:
        async def execute(self, stmt):
            text = str(stmt).lower()
            if text.startswith("update"):
                return FakeResult(rowcount=1)
            if "max(" in text:
                return FakeResult(value=None)
            return FakeResult(scalars=[FakeIncident()])

        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(reconciler, "utc_now", lambda: _at(50))
    import backend.crud as crud
    import backend.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: FakeDB())

    async def fake_timeline(_db, incident_id, **kwargs):
        return None

    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_timeline)

    import sre_agent.war_room_service as wrs

    async def no_slack(incident_id, text):
        return False  # no org token, or Slack rejected it

    monkeypatch.setattr(wrs, "post_to_incident_thread", no_slack)

    with caplog.at_level(logging.ERROR):
        recovered = await reconciler.reconcile_interrupted_remediations(
            threshold=timedelta(minutes=20)
        )

    assert len(recovered) == 1
    assert recovered[0].slack_notified is False
    assert "NO Slack notice" in caplog.text


# ---------------------------------------------------------------------------
# A lapsed approval is the same silence one step earlier: the gate message
# promises "Expires <t>" and nothing ever fires at that time. Live on
# 2026-09-14, five approvals sat `pending` hours past their deadline with the
# incidents stuck in `awaiting_approval` and no further word in Slack.
# ---------------------------------------------------------------------------


def test_the_lapse_notice_says_plainly_that_nothing_ran():
    """The bug this repairs is a human unable to distinguish a silent success
    from a silent lapse, so 'nothing ran' has to be unmissable."""
    from sre_agent.incident_reconciler import lapsed_message

    text = lapsed_message(
        title="[checkout-service] CheckoutMemoryApproachingLimit",
        lapsed_for=timedelta(minutes=168),
    )
    assert "Approval window closed" in text
    assert "168 minute(s) ago" in text
    assert "nothing was run" in text
    assert "cluster is unchanged" in text
    assert "still open" in text
    assert "investigated" in text
    # It must not imply the fix landed or that the incident is done. The bare
    # word "resolved" now appears as the name of the command the notice offers
    # (`mark resolved`), so the guard is on the claim, not the substring.
    assert "is resolved" not in text.lower()
    assert "has been resolved" not in text.lower()
    assert "fixed" not in text.lower()


def test_the_lapse_notice_offers_a_command_the_thread_accepts():
    """It used to end with "re-run the investigation to raise a fresh
    approval". Nothing re-runs an investigation: there is no such war-room
    command, and POST /incidents/trigger dedups on the same title, so the
    only real way forward is to close the incident first."""
    from sre_agent import war_room
    from sre_agent.incident_reconciler import lapsed_message

    text = lapsed_message(
        title="[checkout-service] Whatever", lapsed_for=timedelta(minutes=5)
    )
    assert "mark resolved" in text
    assert war_room.is_resolve_command("mark resolved")


def _lapse_env(monkeypatch, *, approval_claimed=True, other_pending=False,
               incident_claimed=True, slack_ok=True):
    """Fake session covering the lapsed-approval sweep's four statements."""
    import sre_agent.incident_reconciler as reconciler

    state = {"posted": [], "timeline": [], "updates": []}

    class FakeApproval:
        id = "a0000000-0000-4000-8000-000000000001"
        expires_at = _at(20)

    class FakeIncident:
        id = "d3ca5138-7d5a-4f2d-96a7-f5c2958e60d2"
        title = "[checkout-service] CheckoutMemoryApproachingLimit"

    class FakeResult:
        def __init__(self, rows=None, rowcount=0):
            self._rows = rows or []
            self.rowcount = rowcount

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def __init__(self):
            self.committed = 0
            self.rolled_back = 0

        async def execute(self, stmt):
            sql = str(stmt).lower()
            if sql.startswith("update approval_requests"):
                state["updates"].append("approval")
                return FakeResult(rowcount=1 if approval_claimed else 0)
            if sql.startswith("update incidents"):
                state["updates"].append("incident")
                return FakeResult(rowcount=1 if incident_claimed else 0)
            if "join" in sql:
                return FakeResult(rows=[(FakeApproval(), FakeIncident())])
            # The "is any other offer still live?" probe.
            return FakeResult(rows=[("another-id",)] if other_pending else [])

        async def commit(self):
            self.committed += 1

        async def rollback(self):
            self.rolled_back += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    db = FakeDB()
    state["db"] = db
    monkeypatch.setattr(reconciler, "utc_now", lambda: _at(50))

    import backend.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: db)

    import backend.crud as crud

    async def fake_timeline(_db, incident_id, **kwargs):
        state["timeline"].append({"incident_id": incident_id, **kwargs})

    monkeypatch.setattr(crud, "create_incident_timeline_event", fake_timeline)

    import sre_agent.war_room_service as wrs

    async def fake_post(incident_id, text):
        state["posted"].append((incident_id, text))
        return slack_ok

    monkeypatch.setattr(wrs, "post_to_incident_thread", fake_post)
    return reconciler, state


@pytest.mark.asyncio
async def test_a_lapsed_approval_is_retired_and_the_thread_is_told(monkeypatch):
    reconciler, state = _lapse_env(monkeypatch)

    lapsed = await reconciler.reconcile_lapsed_approvals()

    assert len(lapsed) == 1
    assert lapsed[0].new_status == "investigated"
    assert lapsed[0].lapsed_for == timedelta(minutes=30)
    assert lapsed[0].slack_notified is True
    assert state["updates"] == ["approval", "incident"]
    assert state["timeline"][0]["event_type"] == "approval_expired"
    assert "Approval window closed" in state["posted"][0][1]


@pytest.mark.asyncio
async def test_an_incident_with_another_live_offer_keeps_waiting(monkeypatch):
    """Retiring one expired request must not declare the incident unattended
    while a newer request is still genuinely open for a human to answer."""
    reconciler, state = _lapse_env(monkeypatch, other_pending=True)

    lapsed = await reconciler.reconcile_lapsed_approvals()

    assert lapsed == []
    assert state["updates"] == ["approval"]  # the dead row, and nothing else
    assert state["posted"] == []


@pytest.mark.asyncio
async def test_losing_the_claim_race_retires_nothing_and_says_nothing(monkeypatch):
    """Two replicas sweeping at once, or a human deciding in the same instant:
    exactly one may retire the row, and only that one may post."""
    reconciler, state = _lapse_env(monkeypatch, approval_claimed=False)

    lapsed = await reconciler.reconcile_lapsed_approvals()

    assert lapsed == []
    assert state["posted"] == []
    assert state["db"].rolled_back == 1


@pytest.mark.asyncio
async def test_a_failed_slack_post_is_reported_not_swallowed(monkeypatch):
    """Slack is the only channel; an undelivered lapse notice is the original
    bug, so it must be visible rather than reported as success."""
    reconciler, state = _lapse_env(monkeypatch, slack_ok=False)

    lapsed = await reconciler.reconcile_lapsed_approvals()

    assert len(lapsed) == 1
    assert lapsed[0].slack_notified is False


@pytest.mark.asyncio
async def test_each_sweep_runs_even_when_the_other_one_raises(monkeypatch):
    """The sweeps cover different failures; one cannot silence the others."""
    import sre_agent.alert_lifecycle_reconciler as alert_reconciler
    import sre_agent.incident_reconciler as reconciler

    ran: list[str] = []

    async def boom():
        ran.append("interrupted")
        raise RuntimeError("sweep exploded")

    async def ok():
        ran.append("lapsed")
        return []

    async def alerts_ok():
        ran.append("missed_clear")
        reconciler._STOP.set()  # one pass is enough; let the loop fall out
        return []

    monkeypatch.setattr(reconciler, "reconcile_interrupted_remediations", boom)
    monkeypatch.setattr(reconciler, "reconcile_lapsed_approvals", ok)
    monkeypatch.setattr(
        alert_reconciler, "reconcile_missed_alert_resolutions", alerts_ok
    )
    monkeypatch.setattr(reconciler, "sweep_interval", lambda: 30.0)
    reconciler._STOP.clear()

    try:
        await reconciler.reconcile_loop()
    finally:
        reconciler._STOP.clear()

    assert ran == ["interrupted", "lapsed", "missed_clear"]


@pytest.mark.asyncio
async def test_a_dead_row_on_a_moved_on_incident_is_still_retired_quietly(monkeypatch):
    """Live runs left `pending` rows behind on incidents that had already
    finished (2c49ac9d was `resolved` with one still open). The row is dead and
    must be recorded as such, but nobody is waiting, so nobody is paged."""
    reconciler, state = _lapse_env(monkeypatch, incident_claimed=False)

    lapsed = await reconciler.reconcile_lapsed_approvals()

    assert lapsed == []           # nothing announced
    assert state["posted"] == []  # nobody was waiting on it
    assert "approval" in state["updates"]  # but the row is retired
