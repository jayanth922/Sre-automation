#!/usr/bin/env python3
"""Resolution source determines whether an investigation should stop.

Live on 2026-09-14 incident bb5d557e's alert cleared at 17:11:15. Alertmanager
said so, the webhook stamped `resolved_at`, and the investigation carried on
regardless: five more specialists ran, the job was retried after a restart, and
the retry's unconditional `status=INVESTIGATING` write dragged the row back to
`investigating` while `resolved_at` stayed stamped — a row contradicting
itself. Left alone it would have ended where every investigation ends, asking a
human in Slack to approve a cluster write for an alert that had stopped firing.

Human resolution is an intentional stop. An Alertmanager clear is only a
recovery signal: the investigation should finish its findings, but lose all
authority to propose or execute remediation. The runtime must also prevent a
finishing investigation from reopening the resolved incident.

The original defects were:

  * the handler — nothing called `request_job_cancel`. The cancellation
    machinery was complete and unreachable, its only caller a manual HTTP
    endpoint;
  * the write — `_run_graph_impl` sets INVESTIGATING at (re)start without
    looking at what the incident already says, so a run that starts after a
    resolve resurrects it no matter which path did the resolving.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from backend import models


def _job(status: models.JobStatus, *, incident_id: uuid.UUID) -> models.Job:
    return models.Job(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        incident_id=incident_id,
        job_type=models.JobType.INVESTIGATION,
        status=status,
        payload="{}",
        attempt_count=1,
        max_attempts=3,
    )


class _JobSession:
    """Enough AsyncSession for `cancel_incident_investigations`.

    The WHERE clause runs in Postgres, not here, so the rows handed back are
    the ones the query is expected to select. What the fake *can* check is the
    statement itself, which is why it keeps every one it is given.
    """

    def __init__(self, jobs: list[models.Job]) -> None:
        self._jobs = jobs
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        jobs = self._jobs

        class _Result:
            def scalars(self):
                class _Scalars:
                    def all(self):
                        return jobs

                return _Scalars()

        return _Result()

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_resolution_asks_running_and_pending_investigations_to_stop():
    from sre_agent.job_store import cancel_incident_investigations

    incident_id = uuid.uuid4()
    running = _job(models.JobStatus.RUNNING, incident_id=incident_id)
    pending = _job(models.JobStatus.PENDING, incident_id=incident_id)
    db = _JobSession([running, pending])

    cancelled = await cancel_incident_investigations(db, incident_id)

    assert set(cancelled) == {running.id, pending.id}
    assert running.cancel_requested_at is not None
    assert pending.cancel_requested_at is not None
    # The running job keeps its status. Its worker is mid-flight and owns the
    # lease; its own heartbeat reads the flag, raises, and `fail_job` records
    # the CANCELLED. Retiring the row from out here would strand that worker
    # holding a lease on a job nobody will ever finalise.
    assert running.status == models.JobStatus.RUNNING
    assert running.completed_at is None
    # A pending job has no worker to notice anything, so it is retired here.
    assert pending.status == models.JobStatus.CANCELLED
    assert pending.completed_at is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_cancellation_only_looks_at_this_incidents_live_investigations():
    from sre_agent.job_store import cancel_incident_investigations

    incident_id = uuid.uuid4()
    db = _JobSession([])

    cancelled = await cancel_incident_investigations(db, incident_id)

    assert cancelled == []
    # Nothing to do is not a reason to write to the database.
    assert db.commits == 0
    sql = db.statements[0].lower()
    assert "incident_id" in sql
    assert "job_type" in sql
    assert "cancel_requested_at is null" in sql


@pytest.mark.asyncio
async def test_a_second_resolve_does_not_restamp_an_already_cancelled_job():
    """`cancel_requested_at` is the record of when the stop was asked for.

    Two resolve paths can fire for one incident — an Alertmanager clear and a
    human's `mark resolved` in Slack. The second must not move the timestamp
    the first wrote, or the timeline stops matching the jobs table.
    """
    from sre_agent.job_store import cancel_incident_investigations

    incident_id = uuid.uuid4()
    already = _job(models.JobStatus.RUNNING, incident_id=incident_id)
    first_ask = datetime(2026, 9, 14, 17, 11, 15, tzinfo=timezone.utc)
    already.cancel_requested_at = first_ask
    # The query excludes already-flagged jobs, so the session hands back none.
    db = _JobSession([])

    cancelled = await cancel_incident_investigations(db, incident_id)

    assert cancelled == []
    assert already.cancel_requested_at == first_ask


@pytest.mark.asyncio
async def test_every_resolve_path_stops_the_investigation_first(monkeypatch):
    """Cancellation leads the side effects because it is the only one that

    stops work still being done. Closing the war room first would announce the
    incident as finished while its investigation was still spending money and
    still heading for an approval request.
    """
    from backend import database
    from sre_agent import approval_flow, job_store

    order: list[str] = []

    class _NullSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "AsyncSessionLocal", _NullSession)

    async def fake_cancel(_db, _incident_id, **_kw):
        order.append("cancel")
        return [uuid.uuid4()]

    async def fake_close(_incident_id):
        order.append("war_room")

    async def fake_publish(*_a, **_kw):
        order.append("publish")

    async def fake_jira(*_a, **_kw):
        order.append("jira")

    monkeypatch.setattr(job_store, "cancel_incident_investigations", fake_cancel)
    monkeypatch.setattr(
        "sre_agent.war_room_service.close_war_room", fake_close, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.live_events.publish_lifecycle_event", fake_publish, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.integrations.jira.transition_jira_issue", fake_jira, raising=False
    )

    class _Incident:
        id = uuid.uuid4()
        title = "InventorySlowQueries"
        summary = "cleared"

    await approval_flow.fire_resolution_side_effects(
        _Incident(), str(uuid.uuid4()), str(uuid.uuid4())
    )

    assert order == ["cancel", "war_room", "publish", "jira"]


@pytest.mark.asyncio
@pytest.mark.parametrize("jobs_cancelled", [True, False])
async def test_the_thread_is_told_only_when_work_was_actually_stopped(
    monkeypatch, jobs_cancelled
):
    """Slack is the only channel, and silence reads as "still thinking".

    A thread left at ":rotating_light: Incident opened" after the work behind
    it was cancelled is indistinguishable from an investigation in progress —
    the same failure #25b fixed for terminal job failures. The inverse matters
    too: announcing that an investigation was stopped when none was running is
    a claim about the world that is not true.
    """
    from backend import database
    from sre_agent import approval_flow, job_store

    posts: list[str] = []

    class _NullSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "AsyncSessionLocal", _NullSession)

    async def fake_cancel(_db, _incident_id, **_kw):
        return [uuid.uuid4()] if jobs_cancelled else []

    async def fake_post(_incident_id, text):
        posts.append(text)
        return True

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(job_store, "cancel_incident_investigations", fake_cancel)
    monkeypatch.setattr(
        "sre_agent.war_room_service.post_to_incident_thread", fake_post, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.war_room_service.close_war_room", noop, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.live_events.publish_lifecycle_event", noop, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.integrations.jira.transition_jira_issue", noop, raising=False
    )

    class _Incident:
        id = uuid.uuid4()
        title = "InventorySlowQueries"
        summary = "cleared"

    await approval_flow.fire_resolution_side_effects(
        _Incident(), str(uuid.uuid4()), str(uuid.uuid4())
    )

    if jobs_cancelled:
        assert len(posts) == 1
        assert "InventorySlowQueries" in posts[0]
        assert "stopped" in posts[0]
        # The reader's actual question is whether anything is still owed of
        # them. Answer it in the message rather than leaving it inferable.
        assert "No approval will be requested" in posts[0]
    else:
        assert posts == []


@pytest.mark.asyncio
async def test_external_clear_keeps_investigation_but_withdraws_remediation(
    monkeypatch,
):
    """Alertmanager recovery closes authority, not evidence gathering."""
    from sre_agent import approval_flow, job_store

    order: list[str] = []
    posts: list[str] = []

    async def must_not_cancel(*_a, **_kw):
        raise AssertionError("external recovery must not cancel the investigation")

    async def fake_retire(*, incident_id):
        order.append("retire")
        return approval_flow.RetiredRemediationApprovals(
            action_approvals=1,
            gate_approvals=1,
            gate_workflows=(("workflow-1", "start_fix"),),
        )

    async def fake_signal(workflow_id, signal_name, args):
        assert workflow_id == "workflow-1"
        assert signal_name == "decide_start_fix"
        assert args == [False, "Alertmanager clear"]
        order.append("signal")
        return True

    async def fake_post(_incident_id, message):
        order.append("post")
        posts.append(message)
        return True

    async def fake_close(_incident_id):
        order.append("war_room")

    async def fake_publish(*_a, **_kw):
        order.append("publish")

    async def fake_jira(*_a, **_kw):
        order.append("jira")

    monkeypatch.setattr(job_store, "cancel_incident_investigations", must_not_cancel)
    monkeypatch.setattr(
        approval_flow, "retire_pending_remediation_approvals", fake_retire
    )
    monkeypatch.setattr(
        "sre_agent.temporal_client.signal_workflow", fake_signal, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.war_room_service.post_to_incident_thread",
        fake_post,
        raising=False,
    )
    monkeypatch.setattr(
        "sre_agent.war_room_service.close_war_room", fake_close, raising=False
    )
    monkeypatch.setattr(
        "sre_agent.live_events.publish_lifecycle_event",
        fake_publish,
        raising=False,
    )
    monkeypatch.setattr(
        "sre_agent.integrations.jira.transition_jira_issue",
        fake_jira,
        raising=False,
    )

    class _ExternalIncident:
        id = uuid.uuid4()
        title = "InventorySlowQueries"
        summary = "cleared"

    await approval_flow.fire_external_alert_clear_side_effects(
        _ExternalIncident(), str(uuid.uuid4()), str(uuid.uuid4())
    )

    assert order == ["retire", "signal", "post", "war_room", "publish", "jira"]
    assert len(posts) == 1
    assert "investigation will finish" in posts[0]
    assert "approvals were withdrawn" in posts[0]
    assert "no further cluster or repository write will run" in posts[0]


@pytest.mark.asyncio
async def test_alertmanager_resolution_uses_the_external_clear_contract(monkeypatch):
    from backend import crud
    from sre_agent import approval_flow
    from sre_agent.api.v1 import alerts

    incident = _Incident(models.IncidentStatus.INVESTIGATING)
    cluster = type(
        "_Cluster",
        (),
        {"id": uuid.uuid4(), "org_id": uuid.uuid4()},
    )()
    calls: list[str] = []

    async def find_incident(*_a, **_kw):
        return incident

    async def external_clear(resolved_incident, organization_id, cluster_id):
        assert resolved_incident is incident
        assert organization_id == str(cluster.org_id)
        assert cluster_id == str(cluster.id)
        calls.append("external_clear")

    async def timeline(*_a, **_kw):
        calls.append("timeline")

    class _Db:
        async def execute(self, _stmt):
            calls.append("status")
            return None

        async def commit(self):
            calls.append("commit")

    monkeypatch.setattr(crud, "find_active_incident_by_title", find_incident)
    monkeypatch.setattr(crud, "create_incident_timeline_event", timeline)
    monkeypatch.setattr(
        approval_flow,
        "fire_external_alert_clear_side_effects",
        external_clear,
    )

    result = await alerts._reconcile_resolved_alert(
        _Db(),
        cluster,
        {
            "alertname": "InventorySlowQueries",
            "service": "inventory-service",
            "labels": {"alertname": "InventorySlowQueries"},
            "ends_at": "2026-09-15T01:00:00Z",
        },
    )

    assert result["matched"] is True
    assert calls == ["status", "commit", "external_clear", "timeline"]


@pytest.mark.asyncio
async def test_only_the_resolved_status_claim_winner_runs_clear_side_effects(
    monkeypatch,
):
    """A late webhook and background recovery can race on separate replicas.
    The compare-and-set loser must not withdraw or announce twice.
    """
    from backend import crud
    from sre_agent import approval_flow
    from sre_agent.api.v1 import alerts

    incident = _Incident(models.IncidentStatus.INVESTIGATING)
    cluster = type(
        "_Cluster", (), {"id": uuid.uuid4(), "org_id": uuid.uuid4()}
    )()
    calls: list[str] = []

    async def find_incident(*_args, **_kwargs):
        return incident

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("the status claim loser cannot publish side effects")

    class _Result:
        rowcount = 0

    class _Db:
        async def execute(self, _statement):
            calls.append("status")
            return _Result()

        async def commit(self):
            raise AssertionError("a lost claim cannot commit")

        async def rollback(self):
            calls.append("rollback")

    monkeypatch.setattr(crud, "find_active_incident_by_title", find_incident)
    monkeypatch.setattr(crud, "create_incident_timeline_event", must_not_run)
    monkeypatch.setattr(
        approval_flow, "fire_external_alert_clear_side_effects", must_not_run
    )

    result = await alerts._reconcile_resolved_alert(
        _Db(),
        cluster,
        {
            "alertname": "InventorySlowQueries",
            "service": "inventory-service",
            "labels": {"alertname": "InventorySlowQueries"},
        },
    )

    assert result["matched"] is False
    assert result["reason"] == "status_changed_concurrently"
    assert calls == ["status", "rollback"]


class _GuardSession:
    """A session for `_run_graph_impl`'s opening guard.

    `db.get` answers with the incident and the job row under test; `execute`
    records the SQL so a test can say whether the INVESTIGATING write was
    reached.
    """

    def __init__(
        self,
        incident,
        *,
        triggered_by: str = "alertmanager_webhook",
        fail_on_incident_update: bool = False,
    ) -> None:
        self._incident = incident
        self._job = models.Job(
            id=uuid.uuid4(),
            cluster_id=uuid.uuid4(),
            incident_id=incident.id,
            job_type=models.JobType.INVESTIGATION,
            status=models.JobStatus.RUNNING,
            payload=json.dumps({"triggered_by": triggered_by}),
        )
        self._fail_on_incident_update = fail_on_incident_update
        self.statements: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, model, _pk):
        return self._job if model is models.Job else self._incident

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        if self._fail_on_incident_update and sql.lower().startswith("update incidents"):
            raise _ReachedTheWrite()

        class _Result:
            def scalar_one_or_none(self):
                return None

            def scalars(self):
                class _Scalars:
                    def all(self):
                        return []

                    def first(self):
                        return None

                return _Scalars()

        return _Result()

    async def commit(self):
        return None


class _ReachedTheWrite(Exception):
    """Raised by the fake session when the INVESTIGATING update is attempted."""


class _Incident:
    def __init__(self, status) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.title = "InventorySlowQueries"
        self.summary = ""


def _install_guard_session(monkeypatch, session, events):
    from sre_agent import agent_runtime

    # Patch the module objects `agent_runtime` is holding, not whatever
    # `from backend import database` resolves to right now. A module can be
    # re-imported mid-session — `test_config_settings` does exactly that to
    # check `engine.echo` — and the two copies have different engines, so
    # patching the wrong one leaves the code under test talking to a real
    # database and the fake session silently unused.
    monkeypatch.setattr(agent_runtime.database, "AsyncSessionLocal", lambda: session)

    async def fake_event(_db, incident_id, **kwargs):
        events.append((str(incident_id), kwargs.get("event_type")))
        return None

    monkeypatch.setattr(
        agent_runtime.crud, "create_incident_timeline_event", fake_event
    )


@pytest.mark.asyncio
async def test_a_resolved_incident_is_never_dragged_back_to_investigating(
    monkeypatch,
):
    from sre_agent import agent_runtime
    from sre_agent.durable_jobs import DurableJobError

    incident = _Incident(models.IncidentStatus.RESOLVED)
    session = _GuardSession(incident)
    events: list[tuple[str, str]] = []
    _install_guard_session(monkeypatch, session, events)

    job_id = uuid.uuid4()
    with pytest.raises(DurableJobError) as err:
        await agent_runtime._run_graph_impl(
            incident.id, uuid.uuid4(), "InventorySlowQueries", job_id=job_id
        )

    assert "already resolved" in str(err.value)
    # The write that caused the contradiction never happens.
    assert not any(s.lower().startswith("update incidents") for s in session.statements)
    # The job is flagged so the worker's own failure path records CANCELLED
    # rather than burning another of its three attempts on the same dead end.
    assert any(
        "update jobs" in s.lower() and "cancel_requested_at" in s.lower()
        for s in session.statements
    )
    assert events == [(str(incident.id), "investigation_cancelled")]


@pytest.mark.asyncio
async def test_guard_without_a_job_returns_quietly(monkeypatch):
    """The API and mission-control call the runner with no durable job.

    There is nothing to cancel on those paths, so the guard has nothing to
    raise to — it just declines to start and says so in the timeline.
    """
    from sre_agent import agent_runtime

    incident = _Incident(models.IncidentStatus.RESOLVED)
    session = _GuardSession(incident)
    events: list[tuple[str, str]] = []
    _install_guard_session(monkeypatch, session, events)

    result = await agent_runtime._run_graph_impl(
        incident.id, uuid.uuid4(), "InventorySlowQueries"
    )

    assert result is None
    assert events == [(str(incident.id), "investigation_cancelled")]
    assert session.statements == []


@pytest.mark.asyncio
async def test_an_unresolved_incident_still_starts_investigating(monkeypatch):
    """The guard is a backstop, not a new gate.

    Every status but RESOLVED has to pass straight through to the
    INVESTIGATING write — including the ones a re-investigation legitimately
    starts from, like a remediation that failed.
    """
    from sre_agent import agent_runtime

    for status in (
        models.IncidentStatus.OPEN,
        models.IncidentStatus.INVESTIGATING,
        models.IncidentStatus.REMEDIATION_FAILED,
    ):
        incident = _Incident(status)
        session = _GuardSession(incident, fail_on_incident_update=True)
        events: list[tuple[str, str]] = []
        _install_guard_session(monkeypatch, session, events)

        with pytest.raises(_ReachedTheWrite):
            await agent_runtime._run_graph_impl(
                incident.id, uuid.uuid4(), "InventorySlowQueries", job_id=uuid.uuid4()
            )

        assert events == [], f"{status} was wrongly treated as resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("triggered_by", ["manual_trigger", "slack", "dashboard"])
async def test_a_person_may_still_reopen_a_resolved_incident(monkeypatch, triggered_by):
    """The guard refuses machines, not people.

    `mission_control` enqueues an investigation job for any non-chat-only
    reply in an incident's Slack thread, and Slack is the only channel this
    product has. Someone who answers in the thread of a resolved incident has
    asked for the work on purpose; refusing them in a timeline event nobody
    reads would be the same silence the guard exists to stop.
    """
    from sre_agent import agent_runtime

    incident = _Incident(models.IncidentStatus.RESOLVED)
    session = _GuardSession(
        incident, triggered_by=triggered_by, fail_on_incident_update=True
    )
    events: list[tuple[str, str]] = []
    _install_guard_session(monkeypatch, session, events)

    with pytest.raises(_ReachedTheWrite):
        await agent_runtime._run_graph_impl(
            incident.id, uuid.uuid4(), "InventorySlowQueries", job_id=uuid.uuid4()
        )

    assert events == [], f"{triggered_by} was wrongly refused as automatic"


@pytest.mark.asyncio
async def test_reopening_clears_the_resolution_timestamp(monkeypatch):
    """`investigating` and a stamped `resolved_at` cannot both be true.

    That contradiction is what made #28 hard to see in the first place — the
    row said resolved and investigating at once — so the status write names
    `resolved_at` explicitly instead of leaving whatever the last resolve put
    there.
    """
    from sre_agent import agent_runtime

    incident = _Incident(models.IncidentStatus.RESOLVED)
    session = _GuardSession(
        incident, triggered_by="slack", fail_on_incident_update=True
    )
    _install_guard_session(monkeypatch, session, [])

    with pytest.raises(_ReachedTheWrite):
        await agent_runtime._run_graph_impl(
            incident.id, uuid.uuid4(), "InventorySlowQueries", job_id=uuid.uuid4()
        )

    update = session.statements[-1].lower()
    assert update.startswith("update incidents")
    assert "resolved_at" in update, "the status write leaves a stale resolved_at behind"
