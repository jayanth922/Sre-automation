#!/usr/bin/env python3
"""The benchmark must recognise every state the graph stops in.

`pending_acknowledgment` was missing, and it is the *success* state: where a
verified autonomous fix lands, with only a human's Slack "acknowledge" moving
it on to `resolved`. Every terminal *failure* status was already listed. So a
trial in which the agent actually fixed something, but whose Prometheus
recovery probe had not yet cleared, kept polling to the full incident timeout
(2700s) instead of stopping after the grace period — the harness paid 45
minutes of wall clock to learn nothing, and paid it only on the runs that had
gone well.

These pin the set against the graph rather than against a literal, so adding
a terminal status to `compute_incident_status` and forgetting the benchmark
fails here instead of in a paid run.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

from sre_agent import incident_status as incident_status_module

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"

# Reachable from compute_incident_status but genuinely not terminal:
# verification has not run yet, so the graph is going to move again.
TRANSIENT = {"remediation_in_progress"}


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_terminal_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _statuses_the_graph_can_end_on() -> set[str]:
    """Every IncidentStatus `compute_incident_status` can return."""
    source = inspect.getsource(incident_status_module.compute_incident_status)
    names = set(re.findall(r"IncidentStatus\.([A-Z_]+)", source))
    assert names, "the status decision no longer names IncidentStatus members"
    return {
        getattr(incident_status_module.IncidentStatus, name).value for name in names
    }


def test_every_state_the_graph_stops_in_is_terminal_to_the_benchmark(runner):
    unknown = (
        _statuses_the_graph_can_end_on()
        - TRANSIENT
        - runner.TERMINAL_APPLICATION_STATUSES
    )
    assert not unknown, (
        f"the graph can end on {sorted(unknown)}, which the benchmark does not "
        "treat as terminal; such a trial polls to the incident timeout"
    )


def test_a_verified_autonomous_fix_is_a_terminal_state(runner):
    """The success state specifically — the one that was missing."""
    assert "pending_acknowledgment" in runner.TERMINAL_APPLICATION_STATUSES


def test_remediation_in_progress_is_still_treated_as_transient(runner):
    """Verification has not run. Stopping here would record a non-recovery
    for a fix that was still landing."""
    assert "remediation_in_progress" not in runner.TERMINAL_APPLICATION_STATUSES


def test_the_terminal_set_never_decides_recovery(runner):
    """Recovery is the Prometheus probe's call. The status set only stops the
    poll loop early, so every member must be a real IncidentStatus rather
    than a string the graph never emits."""
    known = {status.value for status in incident_status_module.IncidentStatus}
    assert runner.TERMINAL_APPLICATION_STATUSES <= known
