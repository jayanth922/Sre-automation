#!/usr/bin/env python3
"""Unit tests for agent observability (interview Q5)."""

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "sre_agent" / "observability.py"
_spec = importlib.util.spec_from_file_location("observability", _MODULE_PATH)
obs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = obs
_spec.loader.exec_module(obs)


def test_track_records_run_and_duration():
    rec = obs.ObservabilityRecorder()
    with obs.track(rec, "reflector", "inc-1"):
        pass
    summary = rec.summary()
    assert summary["nodes"]["reflector"]["runs"] == 1
    assert summary["total_errors"] == 0


def test_track_captures_failure_trace_and_reraises():
    rec = obs.ObservabilityRecorder()
    with pytest.raises(ValueError):
        with obs.track(rec, "planner", "inc-2"):
            raise ValueError("boom")
    summary = rec.summary()
    assert summary["nodes"]["planner"]["errors"] == 1
    assert summary["nodes"]["planner"]["runs"] == 1
    assert summary["total_errors"] == 1
    assert summary["failures"][0]["node"] == "planner"
    assert "boom" in summary["failures"][0]["detail"]


def test_error_rate_computed():
    rec = obs.ObservabilityRecorder()
    with obs.track(rec, "n", "i"):
        pass
    try:
        with obs.track(rec, "n", "i"):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    s = rec.summary()
    # A failure is a run too: 1 success + 1 failure = 2 runs, 50% error rate.
    assert s["nodes"]["n"]["errors"] == 1
    assert s["nodes"]["n"]["runs"] == 2
    assert s["error_rate"] == 0.5
    assert "provider_switches" not in s


def test_every_top_level_graph_node_is_locally_observed():
    from sre_agent import graph_builder

    source = inspect.getsource(graph_builder.build_multi_agent_graph)
    for node in (
        "prepare",
        "infra_prescan",
        "supervisor",
        "logs_agent",
        "metrics_agent",
        "github_agent",
        "runbooks_agent",
        "aggregate",
        "reflector",
        "investigation_swarm",
        "planner",
        "approval_prepare",
        "approval_gate",
        "act_gate",
    ):
        assert re.search(rf'_observed\(\s*"{node}"', source)


def test_ring_buffer_caps_events():
    rec = obs.ObservabilityRecorder(max_events=5)
    for _ in range(20):
        rec.record(obs.AgentEvent("n", "start"))
    assert len(rec.events()) == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
