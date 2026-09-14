#!/usr/bin/env python3
"""What has to happen when an investigation dies.

Two things, and the platform did neither. A job with attempts left has to go
back on the queue, and when there are no attempts left the on-call has to be
told in Slack — the only channel this platform has.

Live on 2026-09-14 the platform's Anthropic key ran out of credit mid-session.
Three investigations died in a row. Every one of them was written to the jobs
table as FAILED with `attempt_count=1` of `max_attempts=3` and never retried,
and every one of them left a Slack thread reading ":rotating_light: Incident
opened" and then nothing at all — which, to the person reading it, is
indistinguishable from an investigation still in progress.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from backend import models
from sre_agent.job_store import fail_job


class _IdentityMapSession:
    """The part of AsyncSession that made the old guard wrong.

    SQLAlchemy keeps one object per row per session. `db.get()` and the
    `select()` inside `fail_job` therefore hand back the *same* instance, so
    every mutation `fail_job` makes is visible on the caller's own reference
    the moment it makes it. A caller that re-reads that reference to decide
    whether `fail_job` worked is reading `fail_job`'s own writes.
    """

    def __init__(self, job: models.Job) -> None:
        self._job = job
        self.commits = 0

    async def execute(self, _stmt):
        job = self._job

        class _Result:
            def scalars(self):
                class _Scalars:
                    def first(self):
                        return job

                return _Scalars()

        return _Result()

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


def _running_job(*, attempt_count: int, max_attempts: int = 3) -> models.Job:
    return models.Job(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        job_type=models.JobType.INVESTIGATION,
        status=models.JobStatus.RUNNING,
        payload="{}",
        idempotency_key=None,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc),
        cancel_requested_at=None,
        last_error=None,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_a_job_with_attempts_left_goes_back_on_the_queue():
    job = _running_job(attempt_count=1)
    db = _IdentityMapSession(job)

    record = await fail_job(db, job.id, worker_id="worker-a", error="boom")

    assert record.status == models.JobStatus.PENDING
    assert record.completed_at is None, "a job that will retry is not finished"


@pytest.mark.asyncio
async def test_a_job_out_of_attempts_is_dead_lettered():
    job = _running_job(attempt_count=3)
    db = _IdentityMapSession(job)

    record = await fail_job(db, job.id, worker_id="worker-a", error="boom")

    assert record.status == models.JobStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_lease_owner_is_not_evidence_that_fail_job_failed():
    """The trap, pinned.

    The caller held its own `db.get()` reference and used
    `if not job_row.lease_owner:` to mean "fail_job didn't run, write the
    status myself". But `fail_job` nulls `lease_owner` on exactly that object
    on the way to a *successful* return, so the condition was true whether it
    succeeded or not — and the fallback's hard `FAILED` write landed on top of
    the `PENDING` that had just been set. Only the return value distinguishes
    the two cases.
    """
    job = _running_job(attempt_count=1)
    db = _IdentityMapSession(job)
    caller_reference = job

    record = await fail_job(db, job.id, worker_id="worker-a", error="boom")

    assert record.status == models.JobStatus.PENDING
    assert caller_reference.lease_owner is None


# ---------------------------------------------------------------------------
# The caller: what it writes on the row, and what it tells the on-call
# ---------------------------------------------------------------------------

class _UpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _RecordingSession(_IdentityMapSession):
    """Adds the two things `record_investigation_job_failure` needs.

    `db.get()` returns the same instance as the `select()` inside `fail_job`,
    which is the whole point — and `execute()` has to tell a `select` apart
    from the conditional `UPDATE ... WHERE status = 'running'` fallback, and
    honour that WHERE clause, because the fallback landing on a row it does
    not own is the bug under test.
    """

    def __init__(self, job, *, refresh_to=None) -> None:
        super().__init__(job)
        self.updates = []
        self._refresh_to = refresh_to

    async def get(self, _model, _pk):
        return self._job

    async def execute(self, stmt):
        if getattr(stmt, "is_update", False):
            self.updates.append(stmt)
            if self._job is None or self._job.status != models.JobStatus.RUNNING:
                return _UpdateResult(0)
            self._job.status = models.JobStatus.FAILED
            return _UpdateResult(1)
        return await super().execute(stmt)

    async def refresh(self, obj):
        if self._refresh_to is not None:
            obj.status = self._refresh_to


@pytest.mark.asyncio
async def test_a_failure_with_attempts_left_is_not_announced_to_the_on_call():
    """The retry is the answer; telling the thread it failed would be wrong,
    and the next attempt would contradict it."""
    from sre_agent.agent_runtime import record_investigation_job_failure

    job = _running_job(attempt_count=1)
    db = _RecordingSession(job)

    terminal = await record_investigation_job_failure(db, job.id, '{"error":"boom"}')

    assert terminal is False
    assert job.status == models.JobStatus.PENDING
    assert db.updates == [], "fail_job handled it; no fallback write"


@pytest.mark.asyncio
async def test_a_dead_lettered_failure_is_announced():
    from sre_agent.agent_runtime import record_investigation_job_failure

    job = _running_job(attempt_count=3)
    db = _RecordingSession(job)

    assert await record_investigation_job_failure(db, job.id, "{}") is True
    assert job.status == models.JobStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_a_reclaimed_job_is_not_overwritten_with_failed():
    """The second way into the retry bypass.

    No lease can mean the lease reaper got there first, and the reaper returns
    an expired RUNNING job to PENDING for another attempt. The old fallback
    wrote FAILED unconditionally and killed that attempt, so the write is
    conditional on the row still being RUNNING.
    """
    from sre_agent.agent_runtime import record_investigation_job_failure

    job = _running_job(attempt_count=1)
    job.status = models.JobStatus.PENDING  # reaper already requeued it
    job.lease_owner = None
    db = _RecordingSession(job, refresh_to=models.JobStatus.PENDING)

    terminal = await record_investigation_job_failure(db, job.id, "{}")

    assert job.status == models.JobStatus.PENDING, "the requeue must survive"
    assert terminal is False, "a job queued to run again has not finished failing"


@pytest.mark.asyncio
async def test_a_leaseless_job_still_running_is_hard_failed():
    """The fallback still has to work. A RUNNING row nobody else has touched
    is ours to finish, and leaving it RUNNING forever is how a job becomes a
    lease the reaper keeps re-queuing."""
    from sre_agent.agent_runtime import record_investigation_job_failure

    job = _running_job(attempt_count=1)
    job.lease_owner = None
    db = _RecordingSession(job)

    assert await record_investigation_job_failure(db, job.id, "{}") is True
    assert job.status == models.JobStatus.FAILED
    assert len(db.updates) == 1


@pytest.mark.asyncio
async def test_a_job_row_that_vanished_is_still_announced():
    """Nothing is going to retry a job that does not exist, so the thread has
    to hear about it rather than wait for an attempt that never comes."""
    from sre_agent.agent_runtime import record_investigation_job_failure

    db = _RecordingSession(None)

    assert await record_investigation_job_failure(db, uuid.uuid4(), "{}") is True


def test_the_failure_notice_says_what_the_on_call_needs():
    from sre_agent.war_room_service import investigation_failed_text

    text = investigation_failed_text(
        'litellm.BadRequestError: AnthropicException - {"type":"error",'
        '"error":{"message":"Your credit balance is too low"}}'
    )

    # The three things the reader needs: that it failed, that nothing was
    # done, and what went wrong.
    assert "Investigation failed" in text
    assert "still open" in text
    assert "credit balance is too low" in text


def test_the_failure_notice_does_not_paste_a_whole_provider_body():
    from sre_agent.war_room_service import investigation_failed_text

    text = investigation_failed_text("x" * 5000)

    assert len(text) < 800
    assert "…" in text


@pytest.mark.asyncio
async def test_a_thread_with_no_slack_mapping_fails_quietly():
    """Announcing a failure must never become a second failure.

    `post_to_incident_thread` is called from inside the runtime's exception
    handler. If it raised — no org token, no thread, Slack down — it would
    replace the original error with its own and the incident would lose the
    reason it died.
    """
    from sre_agent.war_room_service import post_to_incident_thread

    assert await post_to_incident_thread(str(uuid.uuid4()), "anything") is False
