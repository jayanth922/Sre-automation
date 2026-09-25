#!/usr/bin/env python3
"""The benchmark must say when its own ceiling makes the run meaningless.

`BENCH_INCIDENT_TIMEOUT_SEC` defaults to 300s. A full investigation on the
live cluster has never finished in under 21 minutes (Phase 0, 2026-09-19: 110
LLM calls in 21.1 min). The gap is not a slow-agent measurement — it voids the
trial twice over. The oracle stops waiting and writes UNRESOLVED whatever the
agent did, and harness cleanup then pulls the fault while the agent is still
investigating, so the rest of the run is spent diagnosing a fault that is no
longer there. On incident `e71f7e35` the reflector duly reported the alert was
"not corroborated by any log-level evidence" and dropped to confidence 0.35.

Three pilots on 2026-09-19 were spent before anyone noticed the default had
never been raised, because nothing in the output said so — the timeout was not
even printed. These tests pin the warning that would have caught it on the
first run, and pin the two cases where staying quiet is correct.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_timeout_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_the_default_ceiling_under_a_real_fault_is_called_out(runner):
    """The exact invocation that voided three pilots."""
    warning = runner.timeout_warning("automatic", 300)
    assert warning is not None
    assert "300s" in warning
    assert "non-recovery" in warning
    assert "cleanup will fire mid-investigation" in warning


def test_the_warning_names_the_value_that_fixes_it(runner):
    """A warning that diagnoses without prescribing costs another cycle to act
    on. The operator should be able to copy the fix out of the message."""
    assert "BENCH_INCIDENT_TIMEOUT_SEC=2700" in runner.timeout_warning("automatic", 300)


def test_the_intended_ceiling_is_silent(runner):
    assert runner.timeout_warning("automatic", 2700) is None


def test_the_floor_itself_is_silent(runner):
    """Boundary: the floor is the fastest run ever observed, so a ceiling at
    exactly the floor is tight but not provably unmeasurable."""
    assert runner.timeout_warning("automatic", runner.MEASURED_INCIDENT_FLOOR_SEC) is None
    assert (
        runner.timeout_warning("automatic", runner.MEASURED_INCIDENT_FLOOR_SEC - 1)
        is not None
    )


def test_no_fault_means_no_warning(runner):
    """With nothing injected there is no recovery to wait for, so a short
    ceiling is the right choice rather than a mistake. Warning here would
    train the operator to ignore the line that matters."""
    assert runner.timeout_warning("none", 300) is None


def test_the_floor_is_the_measured_minimum_not_the_maximum(runner):
    """Guards the calibration, which is the whole value of the check.

    Raised toward the 49-minute worst case it would fire on ceilings that can
    legitimately work; lowered toward the 300s default it would go quiet on
    the one invocation it exists to catch. 21 minutes is the fastest complete
    investigation ever observed, so below it the run provably cannot finish.
    """
    assert runner.MEASURED_INCIDENT_FLOOR_SEC == 21 * 60
