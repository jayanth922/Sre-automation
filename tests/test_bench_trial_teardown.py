#!/usr/bin/env python3
"""A trial must not end before the agent answers, and must not leave its
incident open behind it.

Three defects, one test file, because they are the same failure seen from
three sides: the harness stopping too early, the wreckage that leaves in the
platform, and the declaration nobody checks until the money is spent.

* #65 -- an unarmed recovery probe (`require_failure_observation: false`)
  reports recovery on its first observation, because for a negative-control
  scenario the healthy band is where the metric already sits. Breaking the
  poll loop on that ended the trial seconds after the alert fired, before the
  agent had emitted a single span, and the run was graded INSUFFICIENT_EVIDENCE
  for a question it was never given time to answer.

* #66 -- a trial that ends with its incident still open leaves that incident
  in the next scenario's way: a live investigation folds in the next alert for
  the same service, and a repeat run of the same scenario is deduped away by
  its identical title. Either way a trial is lost, and which trials are lost
  depends on timing rather than on the arm, so the damage is not symmetric
  between the arms being compared.

* #68 -- `BENCH_CONFIG_FINGERPRINT` is operator-declared and only shape-checked
  at import. Nothing compared it against a real run manifest until arm
  attestation, which happens after every trial has been paid for.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_teardown_under_test", BENCHMARKS / "sre_bench.py"
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
    def __init__(self, payload=None, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Answers from a fragment -> response table and records what was called."""

    def __init__(self, gets=None, posts=None) -> None:
        self._gets = gets or {}
        self._posts = posts or {}
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    async def get(self, url, **_kwargs):
        self.get_calls.append(url)
        return self._answer(self._gets, "GET", url)

    async def post(self, url, **_kwargs):
        self.post_calls.append(url)
        return self._answer(self._posts, "POST", url)

    @staticmethod
    def _answer(table, verb, url):
        for fragment, response in table.items():
            if fragment in url:
                return response() if callable(response) else response
        raise AssertionError(f"unexpected {verb} {url}")


def _probe(runner, *, require_failure_observation: bool):
    from recovery_oracle import RecoveryProbe

    return RecoveryProbe(
        name="payment_error_ratio",
        query="ratio",
        operator="lt",
        threshold=0.1,
        unit="ratio",
        required_consecutive_passes=2,
        require_failure_observation=require_failure_observation,
    )


def _tracker(runner, probe):
    from recovery_oracle import RecoveryOracleTracker

    tracker = RecoveryOracleTracker(probe, datetime.now(timezone.utc))
    tracker.establish_baseline(0.0)
    return tracker


def _drive_poll_loop(runner, monkeypatch, *, tracker, values, statuses):
    """Run `_wait_for_recovery` against scripted oracle values and statuses."""
    observed: list[float] = []

    async def fake_sleep(_seconds):
        return None

    async def fake_observe(_client, _oracle, trk, *, baseline=False):
        value = values[min(len(observed), len(values) - 1)]
        observed.append(value)
        trk.observe(value)

    async def fake_fetch(_client, _jwt, incident_id, _creds):
        index = min(len(observed) - 1, len(statuses) - 1)
        return {"id": incident_id, "status": statuses[index]}

    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "_observe_oracle", fake_observe)
    monkeypatch.setattr(runner, "_fetch_incident", fake_fetch)
    monkeypatch.setattr(runner, "POLL_INTERVAL_SEC", 5)
    monkeypatch.setattr(runner, "TIMEOUT_SEC", 300)
    monkeypatch.setattr(runner, "ORACLE_COMPLETION_GRACE_SEC", 10)
    monkeypatch.setattr(runner, "AUTO_APPROVE", False)

    latest, approvals = asyncio.run(
        runner._wait_for_recovery(
            _FakeClient(),
            _FakeToken(),
            {"id": "inc-1", "status": "open"},
            object(),
            tracker,
            _Creds(),
        )
    )
    return latest, approvals, observed


# --------------------------------------------------------------------- #65


def test_unarmed_probe_recovery_does_not_end_the_trial(runner, monkeypatch):
    """A negative control runs until the investigation stops, not until the
    metric is healthy -- it was healthy the whole time."""
    tracker = _tracker(runner, _probe(runner, require_failure_observation=False))
    latest, _approvals, observed = _drive_poll_loop(
        runner,
        monkeypatch,
        tracker=tracker,
        values=[0.06] * 12,
        statuses=["investigating"] * 5 + ["pending_acknowledgment"] * 8,
    )

    # The state that used to end the trial on poll 2.
    assert tracker.recovered_at is not None
    assert tracker.failure_observed is False

    assert len(observed) > 2, (
        "the trial stopped as soon as the unarmed probe read healthy, which is "
        "its starting condition; the agent was given no time to investigate"
    )
    assert latest["status"] == "pending_acknowledgment", (
        "the trial should end on the investigation reaching a terminal status, "
        f"not on the oracle; ended on {latest['status']!r}"
    )


def test_armed_probe_still_ends_the_trial_on_verified_recovery(runner, monkeypatch):
    """The #65 fix must not make a real recovery wait for the graph."""
    tracker = _tracker(runner, _probe(runner, require_failure_observation=True))
    latest, _approvals, observed = _drive_poll_loop(
        runner,
        monkeypatch,
        tracker=tracker,
        values=[0.8, 0.8, 0.02, 0.02, 0.02, 0.02],
        statuses=["investigating"] * 20,
    )

    assert tracker.failure_observed is True
    assert tracker.recovered_at is not None
    assert len(observed) <= 5, (
        "an armed probe that verified recovery should stop polling immediately"
    )
    assert latest["status"] == "investigating"


# --------------------------------------------------------------------- #66


def _release(runner, client, incident_id="inc-1"):
    notes: list[str] = []
    asyncio.run(
        runner._release_incident(client, _FakeToken(), incident_id, _Creds(), notes)
    )
    return notes


def test_open_incident_is_closed_at_teardown(runner):
    live = [{"id": "inc-1", "status": "investigating"}]
    client = _FakeClient(
        gets={"/incidents": _FakeResponse(live)},
        posts={"/mark-resolved": _FakeResponse({})},
    )
    notes = _release(runner, client)

    assert client.post_calls == [
        "http://bench.invalid/api/v1/incidents/inc-1/mark-resolved"
    ], "teardown must use the sanctioned endpoint, never a database write"
    assert len(notes) == 1
    assert "closed incident" in notes[0]


def test_parked_incident_is_closed_too(runner):
    """`pending_acknowledgment` stops the investigation but leaves the incident
    open, so a repeat run of the same scenario would be deduped into it."""
    client = _FakeClient(
        gets={
            "/incidents": _FakeResponse(
                [{"id": "inc-1", "status": "pending_acknowledgment"}]
            )
        },
        posts={"/mark-resolved": _FakeResponse({})},
    )
    _release(runner, client)
    assert client.post_calls, "a parked incident still swallows the next alert"


def test_already_resolved_incident_is_left_alone(runner):
    client = _FakeClient(
        gets={"/incidents": _FakeResponse([{"id": "inc-1", "status": "resolved"}])}
    )
    notes = _release(runner, client)
    assert client.post_calls == []
    assert notes == []


def test_teardown_never_raises_into_the_trial(runner):
    """Teardown runs in `finally`; an exception here would mask the trial's own
    result and lose a paid run."""

    class _Exploding(_FakeClient):
        async def get(self, url, **_kwargs):
            raise RuntimeError("network gone")

    notes = _release(runner, _Exploding())
    assert len(notes) == 1
    assert "could not read incident" in notes[0]


def test_teardown_is_a_no_op_without_an_incident(runner):
    assert _release(runner, _FakeClient(), incident_id=None) == []


# --------------------------------------------------------------------- #68


def _manifest(arm: str = "full") -> dict:
    return {
        "provenance": {"code_sha": "13a7a555"},
        "models": {"investigator": "claude-opus-5"},
        "tools": {"io_reference": {"uri": "s3://evidence/run-1"}},
        "runtime": {"ablation_arm": arm, "ablation_experiment": True},
        "trace": {"root_trace_id": "trace-1"},
    }


def _jobs_response(manifest):
    return _FakeResponse(
        [
            {
                "id": "job-1",
                "incident_id": "inc-1",
                "run_manifest": {"manifest": manifest},
            }
        ]
    )


def _verify(runner, client):
    asyncio.run(
        runner._verify_declared_fingerprint(client, _FakeToken(), "inc-1", _Creds())
    )


def test_matching_fingerprint_is_accepted(runner, monkeypatch):
    manifest = _manifest()
    expected = runner.configuration_fingerprint(manifest)
    monkeypatch.setattr(runner, "CONFIG_FINGERPRINT", expected)
    _verify(runner, _FakeClient(gets={"/jobs": _jobs_response(manifest)}))


def test_mismatched_fingerprint_stops_the_campaign(runner, monkeypatch):
    manifest = _manifest()
    actual = runner.configuration_fingerprint(manifest)
    monkeypatch.setattr(runner, "CONFIG_FINGERPRINT", "0" * 64)

    with pytest.raises(SystemExit) as excinfo:
        _verify(runner, _FakeClient(gets={"/jobs": _jobs_response(manifest)}))

    message = str(excinfo.value)
    assert "0" * 64 in message and actual in message, (
        "the abort must name both the declared value and what actually ran"
    )


def test_absent_manifest_is_not_treated_as_a_mismatch(runner, monkeypatch):
    """The job may still be running. Missing evidence is not counter-evidence."""
    monkeypatch.setattr(runner, "CONFIG_FINGERPRINT", "a" * 64)
    _verify(runner, _FakeClient(gets={"/jobs": _FakeResponse([])}))


def test_undeclared_fingerprint_makes_no_request(runner, monkeypatch):
    monkeypatch.setattr(runner, "CONFIG_FINGERPRINT", "")
    client = _FakeClient()
    _verify(runner, client)
    assert client.get_calls == []
