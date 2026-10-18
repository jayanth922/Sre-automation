#!/usr/bin/env python3
"""The harness posts a scenario's alert only once its probe reads failing.

On 2026-10-18 the Act 1 rehearsal posted `CheckoutHighErrorRate` the moment the
bad revision landed. The agent's Step 1 probe, a five-minute rate, then read
0.146 against a 0.45 threshold, and the runbook correctly closed the incident
as sub-threshold. Prometheus fired the real alert four minutes later.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"
STARTED = datetime(2026, 10, 18, 5, 26, 6, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_fault_visible_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class _Oracle:
    """Answers each probe query with the next queued value."""

    def __init__(self, values):
        self.values = list(values)
        self.queries = 0

    async def query(self, client, probe):
        self.queries += 1
        return self.values.pop(0)


def _tracker(runner, *, armed=True, baseline=0.02):
    from recovery_oracle import RecoveryOracleTracker, RecoveryProbe

    probe = RecoveryProbe(
        name="checkout_error_ratio",
        query="ratio",
        operator="lt",
        threshold=0.45,
        unit="ratio",
        required_consecutive_passes=2,
        require_failure_observation=armed,
    )
    tracker = RecoveryOracleTracker(probe, STARTED)
    tracker.establish_baseline(baseline)
    tracker.begin(STARTED)
    return tracker


def _spec(category="bad_deploy"):
    return SimpleNamespace(name="checkout_bad_config_deploy", taxonomy={"category": category})


def _visible(runner, oracle, tracker, spec, budget=360):
    return asyncio.run(
        runner._await_fault_visible(
            None, oracle, tracker, spec, STARTED, budget_seconds=budget, poll_seconds=0
        )
    )


def test_alert_waits_for_the_window_to_fill(runner):
    oracle = _Oracle([0.03, 0.146, 0.31, 0.53])
    tracker = _tracker(runner)

    alert_at = _visible(runner, oracle, tracker, _spec())

    assert oracle.queries == 4
    assert tracker.failure_observed
    first_failing = tracker.observations[-1]
    assert first_failing.value == 0.53 and first_failing.state == "failing"
    assert alert_at == first_failing.observed_at
    assert alert_at > STARTED
    # The trial clock still measures from the fault, not from the alert.
    assert tracker.started_at == STARTED


def test_negative_control_posts_at_once(runner):
    oracle = _Oracle([])
    tracker = _tracker(runner)

    assert _visible(runner, oracle, tracker, _spec("clean")) == STARTED
    assert oracle.queries == 0


def test_unarmed_probe_posts_at_once(runner):
    oracle = _Oracle([])
    tracker = _tracker(runner, armed=False)

    assert _visible(runner, oracle, tracker, _spec()) == STARTED
    assert oracle.queries == 0


def test_unhealthy_baseline_posts_at_once(runner):
    oracle = _Oracle([])
    tracker = _tracker(runner, baseline=0.9)

    assert _visible(runner, oracle, tracker, _spec()) == STARTED
    assert oracle.queries == 0


def test_zero_budget_restores_posting_at_injection(runner):
    oracle = _Oracle([])
    tracker = _tracker(runner)

    assert _visible(runner, oracle, tracker, _spec(), budget=0) == STARTED
    assert oracle.queries == 0


def test_fault_that_never_shows_fails_the_stimulus(runner, monkeypatch):
    clock = iter([0.0, 100.0, 200.0, 400.0])
    # Replace only the runner's reference: asyncio reads the real clock too.
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    oracle = _Oracle([0.05, 0.08, 0.1])
    tracker = _tracker(runner)

    with pytest.raises(RuntimeError, match="never showed on probe checkout_error_ratio"):
        _visible(runner, oracle, tracker, _spec())
    assert not tracker.failure_observed
    assert oracle.queries == 3


def test_unreadable_probe_keeps_waiting(runner):
    oracle = _Oracle([None, 0.6])
    tracker = _tracker(runner)

    _visible(runner, oracle, tracker, _spec())

    assert [o.state for o in tracker.observations[1:]] == ["unknown", "failing"]
