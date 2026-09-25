#!/usr/bin/env python3
"""#74 -- a same-service fold must not straddle the scenario boundary.

Folding a second alert for the same service into an already-open incident is
correct production behaviour, validated 12/12 in shadow mode. It is wrong only
in the benchmark, and only because of a shape production never has:
consecutive independent scenarios, on the same handful of services, minutes
apart. Two things follow from that, and this file holds both.

* What the harness *can* prevent, it must assert rather than assume. #66's
  teardown closes the incident each trial opened, which is not the only way one
  can be open -- a previous campaign, a manual run, or a trial killed before
  its `finally` all leave one behind. If one is open when the alert fires, the
  alert folds into it, no new incident appears, `_wait_new_incident` times out,
  and a paid trial is spent measuring nothing. That is exactly the
  `platform_failure` with zero spans seen once in #28 and twice in #70.

* What it cannot prevent, it must record. An alert arriving *during* a trial --
  the previous scenario's fault re-firing after its incident was closed,
  7-9 minutes into the next one in both observed cases -- cannot be stopped
  from outside the platform, and stopping it inside would mean shipping a
  correlation rule written for a benchmark. So the trial is marked
  contaminated: the incident being graded contains a stimulus this scenario
  never fired, and whatever the agent did about it is not attributable here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_boundary_under_test", BENCHMARKS / "sre_bench.py"
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
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """Routes by URL so the test can assert what was actually closed."""

    def __init__(self, incidents):
        self._incidents = {inc["id"]: dict(inc) for inc in incidents}
        self.resolved: list[str] = []

    async def get(self, url, **_):
        if url.endswith("/incidents"):
            return _FakeResponse(list(self._incidents.values()))
        incident_id = url.rsplit("/", 1)[-1]
        return _FakeResponse(self._incidents.get(incident_id))

    async def post(self, url, **_):
        incident_id = url.rsplit("/", 2)[-2]
        self.resolved.append(incident_id)
        self._incidents[incident_id]["status"] = "resolved"
        return _FakeResponse({})


def test_an_incident_left_open_is_closed_before_the_alert_fires(runner):
    client = _FakeClient(
        [
            {"id": "aaaaaaaa-0000", "status": "investigating"},
            {"id": "bbbbbbbb-1111", "status": "resolved"},
            {"id": "cccccccc-2222", "status": "awaiting_approval"},
        ]
    )
    notes: list[str] = []

    asyncio.run(
        runner._settle_open_incidents(client, _FakeToken(), _Creds(), notes)
    )

    # The two that could have absorbed this trial's alert, and only those.
    assert sorted(client.resolved) == ["aaaaaaaa-0000", "cccccccc-2222"]
    assert any("open before this trial fired" in note for note in notes)


def test_a_quiet_cluster_is_left_alone(runner):
    client = _FakeClient([{"id": "aaaaaaaa-0000", "status": "resolved"}])
    notes: list[str] = []

    asyncio.run(
        runner._settle_open_incidents(client, _FakeToken(), _Creds(), notes)
    )

    assert client.resolved == []
    assert notes == []


def test_the_precondition_never_raises_into_the_trial(runner):
    """A harness precondition that can abort a campaign is worse than the fold.

    It runs before every trial, so a transient list failure must degrade to a
    note, not to a lost run.
    """

    class _Broken:
        async def get(self, *_, **__):
            raise RuntimeError("connection reset")

    notes: list[str] = []
    asyncio.run(runner._settle_open_incidents(_Broken(), _FakeToken(), _Creds(), notes))

    assert any("could not list open incidents" in note for note in notes)


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "correlated_alert_folded"},
        {"title": "Correlated alert folded into this incident"},
        {"type": "CORRELATED_ALERT_FOLDED"},
    ],
)
def test_a_fold_during_the_trial_is_counted(runner, event):
    assert runner._folded_alert_count([event]) == 1


def test_an_ordinary_timeline_is_not_mistaken_for_a_fold(runner):
    events = [
        {"event_type": "investigation_started", "title": "Investigation started"},
        {"event_type": "action_approved", "title": "Operator approved restart"},
        {"title": "Recovery verified"},
        "not-a-dict",
        None,
    ]

    assert runner._folded_alert_count(events) == 0
    assert runner._folded_alert_count([]) == 0
    assert runner._folded_alert_count(None) == 0


def test_contamination_reaches_the_trial_record(runner, monkeypatch):
    """`cross_scenario_fold` has to survive into the artifact people diff.

    A contaminated trial that looks identical to a clean one is worse than no
    trial: it is averaged into an arm and moves a number nobody can trace back.
    """
    captured = {}

    monkeypatch.setattr(runner, "STATISTICAL_RECORDING", True)
    monkeypatch.setattr(runner, "EXPERIMENT_ID", "boundary-test")
    monkeypatch.setattr(runner, "CANDIDATE_ID", "full")
    monkeypatch.setattr(runner, "CONFIG_FINGERPRINT", "f" * 64)
    monkeypatch.setattr(runner, "PAIR_SEED", "boundary-seed")
    monkeypatch.setattr(runner, "append_trial", lambda path, record: None)
    monkeypatch.setattr(
        runner,
        "build_trial_record",
        lambda **kwargs: captured.update(kwargs) or {},
    )

    spec = next(iter(runner.SCENARIOS), None)
    if spec is None:  # pragma: no cover - the corpus is never empty
        pytest.skip("no scenarios loaded")

    score = runner.score_run(
        spec,
        "UNRESOLVED",
        "investigating",
        "",
        [],
        mttr_seconds=None,
        incident_severity="",
    )
    runner._record_statistical_trial(
        spec,
        score,
        trial_index=1,
        latency_seconds=1.0,
        trace_completeness={"complete": True, "cost_usd": 0.5, "spans": 40},
        extra_categories=("cross_scenario_fold",),
    )

    assert "cross_scenario_fold" in captured["failure_categories"]
    # And the cost is no longer collateral damage of the span tree (#73).
    assert captured["cost_usd"] == 0.5
