#!/usr/bin/env python3
"""Who is allowed to say an investigation finished.

Exactly one owner per outcome, and for the *failing* half of a run that was
already true: `job_store.fail_job` refuses a job whose lease has moved on, and
`record_investigation_job_failure` falls back to a write conditional on the
row still being RUNNING. Its docstring names two live cases (2026-09-14) where
skipping that check silently cancelled a retry the queue had already granted.

The succeeding half had none of it. `_run_graph_impl` stamped
COMPLETED/DEGRADED with `WHERE jobs.id = :id` and nothing else -- the same bug
on the branch nobody looked at, and the one that runs on every healthy
investigation. Two ways in:

  * A lease that lapses mid-run -- a paused container, a DB blip, an event
    loop starved by a long model call. `reclaim_expired_leases` returns the
    row to PENDING and another worker claims it, so two runs are now
    investigating one incident. The first to finish stamps COMPLETED over the
    second one's live RUNNING row; the second's own `fail_job` then finds a
    row it does not own, raises, and `_execute_and_finalize` swallows it. The
    duplicate run leaves no trace anywhere.
  * A cancel arriving mid-run. `cancel_incident_investigations` deliberately
    only sets `cancel_requested_at` on a RUNNING job and leaves the terminal
    write to the worker, which turns it into CANCELLED. A COMPLETED stamp
    getting there first strands the flag permanently: `fail_job` cannot reach
    a row that is no longer RUNNING.

For a paid benchmark campaign the first one is the expensive failure -- two
trials billed, one recorded, and nothing in the data saying so.

`job_store.complete_job` enforces this by raising. The runtime cannot use it:
it owns the successful transition on purpose, because it is the only place
holding the incident status, the verification outcome and the dashboard
payload, and it writes them in the incident's own transaction. So it enforces
the same rule as a WHERE clause instead, which is also the only form that
cannot race -- a Python-side ownership check re-reads a row another worker may
already have taken.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend import models


class _WriteRecordingSession:
    """Captures the UPDATE and answers with a scripted rowcount.

    A fake cannot evaluate a WHERE clause, so the two halves are checked
    separately: the statement's own compiled SQL says the guard is *asked*
    for, and the rowcount says what the caller does with the answer.
    """

    def __init__(self, rowcount: int = 1) -> None:
        self._rowcount = rowcount
        self.statements: list = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        self.commits += 1


async def _write(db, *, worker_id):
    from sre_agent.agent_runtime import record_investigation_job_success

    return await record_investigation_job_success(
        db,
        uuid.uuid4(),
        worker_id=worker_id,
        status=models.JobStatus.COMPLETED,
        result_json='{"summary": "the connection pool was saturated"}',
        now=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )


def _sql(db) -> str:
    assert len(db.statements) == 1, "exactly one write, or the guard is bypassable"
    return str(db.statements[0].compile())


@pytest.mark.asyncio
async def test_the_completion_write_names_every_owner_it_depends_on():
    """All three predicates, in the statement, where a race cannot get between
    reading them and acting on them."""
    db = _WriteRecordingSession(rowcount=1)

    assert await _write(db, worker_id="worker-a") is True

    sql = _sql(db)
    assert "jobs.id = " in sql
    assert "jobs.status = " in sql, "a row the reaper returned to PENDING is not ours"
    assert "jobs.lease_owner = " in sql, "a row another worker re-claimed is not ours"
    assert "jobs.cancel_requested_at IS NULL" in sql, "a cancel outranks a completion"

    bound = db.statements[0].compile().params
    assert models.JobStatus.RUNNING in bound.values()
    assert "worker-a" in bound.values()
    assert models.JobStatus.COMPLETED in bound.values()


@pytest.mark.asyncio
async def test_a_run_whose_job_moved_on_does_not_report_success():
    """Zero rows matched is the whole signal. Before this, the write could not
    fail: it addressed the row by primary key and always found it."""
    db = _WriteRecordingSession(rowcount=0)

    assert await _write(db, worker_id="worker-a") is False


@pytest.mark.asyncio
async def test_an_unleased_run_still_cannot_overwrite_a_decided_row():
    """Nobody leased this one -- the quarantined entry point, or a direct
    call. There is no owner to check, but PENDING and CANCELLED are still the
    queue's decisions and not this run's to revoke."""
    db = _WriteRecordingSession(rowcount=1)

    assert await _write(db, worker_id=None) is True

    sql = _sql(db)
    assert "jobs.lease_owner" not in sql
    assert "jobs.status = " in sql
    assert "jobs.cancel_requested_at IS NULL" in sql


@pytest.mark.asyncio
async def test_the_completion_does_not_commit_on_its_own():
    """It shares a transaction with the incident write. Committing here would
    publish a finished job whose incident may still roll back."""
    db = _WriteRecordingSession(rowcount=1)

    await _write(db, worker_id="worker-a")

    assert db.commits == 0


def test_the_runtime_hands_a_lost_job_back_to_the_queue():
    """A wiring check, not a behavioural one: reaching that branch for real
    needs a full graph run. What it pins is that the runtime asks the guarded
    writer and raises on a no -- the worker's failure path is what knows
    whether this was a cancel or a lost lease, and it only gets to decide if
    the run declines to answer for itself."""
    from sre_agent import agent_runtime

    source = inspect.getsource(agent_runtime._run_graph_impl)
    assert "record_investigation_job_success(" in source
    assert "if not job_is_still_ours:" in source
    assert "raise DurableJobError(" in source
    # The opening RUNNING/started_at write a few hundred lines up addresses
    # the row by primary key alone, and should: the worker has just claimed it
    # and is saying the run began. What may never live here again is a write
    # that *ends* the job, and `completed_at` is what makes one terminal.
    assert "completed_at" not in source, "an unguarded terminal write came back"


def test_there_is_no_convenient_unguarded_job_status_setter():
    """`backend.crud.update_job_status` set any status on any job with no
    lease check and had no callers. A trap, not a live hole -- and the fix for
    a trap is to remove it, not to guard a function nothing calls."""
    from backend import crud

    assert not hasattr(crud, "update_job_status")

    setters = [
        name
        for name in dir(crud)
        if "job" in name.lower() and ("status" in name.lower() or "complete" in name.lower())
    ]
    assert setters == [], f"a job-status writer reappeared in crud: {setters}"
