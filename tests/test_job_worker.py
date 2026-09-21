#!/usr/bin/env python3
"""Durable investigation worker lease regression tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from sre_agent import job_worker


@pytest.mark.asyncio
async def test_lease_renewer_heartbeats_repeatedly(monkeypatch):
    stop = asyncio.Event()
    calls = []

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_heartbeat(db, job_id, *, worker_id, lease_seconds):
        calls.append((job_id, worker_id, lease_seconds))
        if len(calls) == 2:
            stop.set()

    monkeypatch.setattr(
        job_worker.database, "AsyncSessionLocal", lambda: FakeSessionContext()
    )
    monkeypatch.setattr(job_worker, "heartbeat_job", fake_heartbeat)
    job_id = uuid.uuid4()

    await job_worker._renew_job_lease(
        job_id,
        worker_id="worker-a",
        lease_seconds=60,
        stop=stop,
        renewal_interval=0.001,
    )

    assert calls == [
        (job_id, "worker-a", 60),
        (job_id, "worker-a", 60),
    ]


@pytest.mark.asyncio
async def test_execute_claimed_job_leaves_success_completion_to_runtime(monkeypatch):
    """The runtime owns the rich terminal job write, not the queue worker."""
    job = _job()
    job.payload.update(
        {
            "incident_id": str(job.incident_id),
            "cluster_id": str(job.cluster_id),
            "alert_name": "ApiLatencyHigh",
        }
    )
    heartbeat_calls = []
    runtime_calls = []

    async def fake_heartbeat(db, job_id, *, worker_id, lease_seconds):
        heartbeat_calls.append((job_id, worker_id, lease_seconds))

    async def fake_run(**kwargs):
        runtime_calls.append(kwargs)

    async def forbidden_complete(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("job_worker must not complete the runtime-owned job")

    monkeypatch.setattr(job_worker, "heartbeat_job", fake_heartbeat)
    monkeypatch.setattr(
        "sre_agent.incident_runner.run_incident_investigation", fake_run
    )
    # Keep this guard even though the production import was removed: it makes
    # a future reintroduction of the second completion owner fail loudly.
    monkeypatch.setattr(job_worker, "complete_job", forbidden_complete, raising=False)
    monkeypatch.setattr(
        job_worker.database, "AsyncSessionLocal", lambda: _FakeSessionContext()
    )

    await job_worker.execute_claimed_job(job, worker_id="worker-a")

    assert heartbeat_calls == [(job.id, "worker-a", 60)]
    assert runtime_calls == [
        {
            "incident_id": job.incident_id,
            "cluster_id": job.cluster_id,
            "alert_name": "ApiLatencyHigh",
            "job_id": job.id,
            "alert_labels": {},
            "alert_annotations": {},
            "alert_starts_at": None,
            "alert_severity": "warning",
            "organization_id": str(job.organization_id),
            "admission_owner": "worker-a",
        }
    ]


# --- A claimed batch must not starve behind its own first member -------------
# `claim_jobs` marks every job in the batch RUNNING with a lease that starts
# ticking at claim time, but only `execute_claimed_job` starts a job's lease
# renewal. Awaiting the batch one job at a time therefore left the queued
# siblings holding leases nobody renewed; by the time their turn came the
# opening heartbeat raised "lease expired" and failed them into a retry — a
# second full investigation of work the first attempt had already done.


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _job(job_id=None):
    from sre_agent.job_store import DurableJob

    return DurableJob(
        id=job_id or uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        job_type="investigation",
        status="running",
        payload={"handler": "run_graph_background_saas"},
        attempt_count=1,
        max_attempts=3,
    )


@pytest.fixture
def batch_worker(monkeypatch):
    """`worker_loop` wired to a one-shot claim of a given batch."""
    started: list[uuid.UUID] = []
    finished: list[uuid.UUID] = []
    failed: list[tuple[uuid.UUID, str]] = []
    release = asyncio.Event()

    def configure(jobs, *, first_blocks=True):
        claimed_once = {"done": False}

        async def fake_claim(db, *, worker_id, limit, lease_seconds):
            if claimed_once["done"]:
                job_worker._STOP.set()
                return []
            claimed_once["done"] = True
            return list(jobs)

        async def fake_execute(job, *, worker_id):
            started.append(job.id)
            # The first job is the long investigation the others queued behind.
            if first_blocks and job.id == jobs[0].id:
                await release.wait()
            finished.append(job.id)

        async def fake_fail(db, job_id, *, worker_id, error):
            failed.append((job_id, error))

        monkeypatch.setattr(job_worker, "claim_jobs", fake_claim)
        monkeypatch.setattr(job_worker, "execute_claimed_job", fake_execute)
        monkeypatch.setattr(job_worker, "fail_job", fake_fail)
        monkeypatch.setattr(
            job_worker.database, "AsyncSessionLocal", lambda: _FakeSessionContext()
        )
        monkeypatch.setattr(job_worker, "_poll_interval", lambda: 0.001)
        monkeypatch.setattr(job_worker, "_batch_size", lambda: 10)
        monkeypatch.setattr(job_worker, "default_lease_seconds", lambda: 60)

        class FakeAdmission:
            def stats(self):
                return {"available": 10}

        import sre_agent.concurrency as concurrency

        monkeypatch.setattr(
            concurrency, "get_admission_controller", lambda: FakeAdmission()
        )

    job_worker._STOP.clear()
    yield configure, started, finished, failed, release
    job_worker._STOP.set()


@pytest.mark.asyncio
async def test_a_long_job_does_not_hold_up_the_rest_of_its_batch(batch_worker):
    configure, started, finished, failed, release = batch_worker
    jobs = [_job(), _job(), _job()]
    configure(jobs)

    loop_task = asyncio.create_task(job_worker.worker_loop("worker-a"))
    # The siblings must be under way while the first job is still blocked.
    for _ in range(200):
        if len(started) == 3:
            break
        await asyncio.sleep(0.005)

    assert [j.id for j in jobs[1:]] == [i for i in started if i != jobs[0].id], (
        "queued siblings never started while the first job ran; they sit with "
        "un-renewed leases until the heartbeat rejects them"
    )
    assert set(finished) == {jobs[1].id, jobs[2].id}

    release.set()
    await asyncio.wait_for(loop_task, timeout=5)
    assert set(finished) == {j.id for j in jobs}
    assert failed == []


@pytest.mark.asyncio
async def test_one_failing_job_does_not_cancel_its_siblings(batch_worker, monkeypatch):
    """A batch is gathered, so a raise in one member must be recorded on that
    job alone — never propagated into the loop or its siblings."""
    configure, started, finished, failed, release = batch_worker
    jobs = [_job(), _job()]
    configure(jobs, first_blocks=False)

    async def explode(job, *, worker_id):
        started.append(job.id)
        if job.id == jobs[0].id:
            raise job_worker.DurableJobError("lease expired")
        finished.append(job.id)

    monkeypatch.setattr(job_worker, "execute_claimed_job", explode)
    await asyncio.wait_for(job_worker.worker_loop("worker-a"), timeout=5)

    assert finished == [jobs[1].id]
    assert [jid for jid, _ in failed] == [jobs[0].id]
    assert failed[0][1] == "lease expired"


# --- A resolved incident has to actually stop the run ------------------------
# `cancel_incident_investigations` stamps `cancel_requested_at` and leaves a
# RUNNING job alone on purpose: the worker owns the lease, so the worker has to
# be the one that retires it. Three separate comments assert that the heartbeat
# is what carries the flag across that boundary. Nothing exercised it, which
# for a stop button is the one property worth a test.


@pytest.mark.asyncio
async def test_a_cancel_request_stops_the_investigation_mid_flight(monkeypatch):
    job = _job()
    job.payload.update(
        {
            "incident_id": str(job.incident_id),
            "cluster_id": str(job.cluster_id),
            "alert_name": "ApiLatencyHigh",
        }
    )
    beats = {"count": 0}
    run_was_cancelled = asyncio.Event()

    async def fake_heartbeat(db, job_id, *, worker_id, lease_seconds):
        beats["count"] += 1
        if beats["count"] > 1:
            # A human marked the incident resolved between the two beats.
            raise job_worker.DurableJobError("job cancellation requested")

    async def never_finishes(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run_was_cancelled.set()
            raise

    monkeypatch.setattr(job_worker, "heartbeat_job", fake_heartbeat)
    monkeypatch.setattr(
        "sre_agent.incident_runner.run_incident_investigation", never_finishes
    )
    monkeypatch.setattr(
        job_worker.database, "AsyncSessionLocal", lambda: _FakeSessionContext()
    )
    monkeypatch.setattr(job_worker, "default_lease_seconds", lambda: 3)

    with pytest.raises(job_worker.DurableJobError, match="cancellation requested"):
        await job_worker.execute_claimed_job(job, worker_id="worker-a")

    # Not merely "the worker stopped waiting" -- the investigation task itself
    # was cancelled, so it stops burning tokens on an incident that is over.
    assert run_was_cancelled.is_set()


@pytest.mark.asyncio
async def test_a_cancelled_run_is_handed_to_fail_job_rather_than_raised(monkeypatch):
    """`fail_job` is what writes CANCELLED, so the error has to reach it."""
    job = _job()
    failed: list[tuple] = []

    async def cancelled(job_arg, *, worker_id):
        raise job_worker.DurableJobError(
            "job lease renewal failed: job cancellation requested"
        )

    async def fake_fail(db, job_id, *, worker_id, error):
        failed.append((job_id, worker_id, error))

    monkeypatch.setattr(job_worker, "execute_claimed_job", cancelled)
    monkeypatch.setattr(job_worker, "fail_job", fake_fail)
    monkeypatch.setattr(
        job_worker.database, "AsyncSessionLocal", lambda: _FakeSessionContext()
    )

    was_stopped = job_worker._STOP.is_set()
    job_worker._STOP.clear()
    try:
        await job_worker._execute_and_finalize(job, "worker-a")
    finally:
        if was_stopped:
            job_worker._STOP.set()

    assert failed == [
        (
            job.id,
            "worker-a",
            "job lease renewal failed: job cancellation requested",
        )
    ]
