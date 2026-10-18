#!/usr/bin/env python3
"""The harness stops paying for a campaign's aftermath before it exits.

On 2026-10-17 a trial exited at $1.44 and its cascade kept going: an alert the
fault raised on a calling service had become its own incident, and that
incident's investigation spent another $1.00 after the bench had printed its
result. Two more alerts, still inside their five-minute rate windows, opened
incidents after that. Teardown only knew about the trial's own incident.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"
STARTED = datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_quiesce_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class _Creds:
    base_url = "http://bench.invalid"
    cluster_id = "cluster-1"


class _FakeToken:
    async def headers(self) -> dict:
        return {"Authorization": "Bearer test"}


class _FakeResponse:
    def __init__(self, payload=None) -> None:
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _at(minutes: int) -> str:
    return (STARTED + timedelta(minutes=minutes)).isoformat()


class _Cluster:
    """Incidents, jobs and Prometheus ALERTS behind the bench's HTTP calls.

    `alerts` is consumed one value per poll, then holds its last value. A
    cancel takes effect on the next poll, the way a worker notices it on its
    next lease heartbeat.
    """

    def __init__(self, incidents, jobs, alerts):
        self.incidents = {inc["id"]: dict(inc) for inc in incidents}
        self.jobs = {job["id"]: dict(job) for job in jobs}
        self.alerts = list(alerts)
        self.resolved: list[str] = []
        self.cancelled: list[str] = []
        self._stopping: set[str] = set()

    async def get(self, url, **kwargs):
        if url.endswith("/api/v1/query"):
            value = self.alerts.pop(0) if len(self.alerts) > 1 else self.alerts[0]
            return _FakeResponse(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"value": [0, str(value)]}],
                    },
                }
            )
        if url.endswith("/incidents"):
            return _FakeResponse(list(self.incidents.values()))
        if url.endswith("/jobs"):
            for job_id in self._stopping:
                self.jobs[job_id]["status"] = "cancelled"
            self._stopping.clear()
            return _FakeResponse(list(self.jobs.values()))
        return _FakeResponse(self.incidents.get(url.rsplit("/", 1)[-1]))

    async def post(self, url, **_):
        if url.endswith("/cancel"):
            job_id = url.rsplit("/", 2)[-2]
            self.cancelled.append(job_id)
            self._stopping.add(job_id)
            return _FakeResponse({})
        incident_id = url.rsplit("/", 2)[-2]
        self.resolved.append(incident_id)
        self.incidents[incident_id]["status"] = "resolved"
        for job in self.jobs.values():
            if job["incident_id"] == incident_id and job["status"] in {"pending", "running"}:
                self._stopping.add(job["id"])
        return _FakeResponse({})


def _quiesce(runner, cluster, *, known_before=(), budget=60.0):
    return asyncio.run(
        runner._quiesce_campaign(
            cluster,
            _FakeToken(),
            _Creds(),
            started_at=STARTED,
            known_before=set(known_before),
            budget_seconds=budget,
            poll_seconds=0,
        )
    )


def test_aftermath_incidents_and_their_investigations_are_stopped(runner):
    cluster = _Cluster(
        incidents=[
            {"id": "pre-existing-0", "status": "investigating"},
            {"id": "trial-own-0001", "status": "resolved"},
            # Opened by an alert the fault raised on a calling service.
            {"id": "cascade-000002", "status": "investigating"},
            # Cleared by Alertmanager, which by design does not cancel its job.
            {"id": "cleared-000003", "status": "resolved"},
        ],
        jobs=[
            {"id": "job-old-00", "incident_id": "pre-existing-0", "status": "running", "created_at": _at(-5)},
            {"id": "job-own-01", "incident_id": "trial-own-0001", "status": "completed", "created_at": _at(1)},
            {"id": "job-cas-02", "incident_id": "cascade-000002", "status": "running", "created_at": _at(9)},
            {"id": "job-clr-03", "incident_id": "cleared-000003", "status": "running", "created_at": _at(10)},
        ],
        alerts=[2, 1, 0],
    )

    notes = _quiesce(runner, cluster, known_before={"pre-existing-0"})

    assert cluster.resolved == ["cascade-000002"]
    # mark-resolved stopped the cascade's job; only the job under the
    # externally cleared incident needed cancelling directly.
    assert cluster.cancelled == ["job-clr-03"]
    assert cluster.jobs["job-cas-02"]["status"] == "cancelled"
    assert cluster.jobs["job-clr-03"]["status"] == "cancelled"
    assert cluster.incidents["pre-existing-0"]["status"] == "investigating"
    assert cluster.jobs["job-old-00"]["status"] == "running"
    assert not any("check spend" in note for note in notes)


def test_a_job_is_cancelled_once_even_while_it_takes_time_to_stop(runner):
    cluster = _Cluster(
        incidents=[{"id": "cleared-000003", "status": "resolved"}],
        jobs=[{"id": "job-clr-03", "incident_id": "cleared-000003", "status": "running", "created_at": _at(10)}],
        alerts=[1, 1, 1, 0],
    )

    _quiesce(runner, cluster)

    assert cluster.cancelled == ["job-clr-03"]


def test_running_out_of_budget_says_so(runner):
    cluster = _Cluster(incidents=[], jobs=[], alerts=[1])

    notes = _quiesce(runner, cluster, budget=0.001)

    assert any("1 alert(s) active" in note and "check spend" in note for note in notes)


def test_an_unreadable_alert_count_is_not_taken_for_quiet(runner):
    class _NoPrometheus(_Cluster):
        async def get(self, url, **kwargs):
            if url.endswith("/api/v1/query"):
                raise RuntimeError("connection refused")
            return await super().get(url, **kwargs)

    notes = _quiesce(runner, _NoPrometheus([], [], [0]), budget=0.001)

    assert any("unknown alert(s) active" in note for note in notes)


def test_zero_budget_disables_the_guard(runner):
    class _Untouchable:
        async def get(self, *_, **__):
            raise AssertionError("quiesce disabled but the cluster was read")

        post = get

    assert _quiesce(runner, _Untouchable(), budget=0) == []
