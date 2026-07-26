#!/usr/bin/env python3
"""Unit tests for agent observability (interview Q5)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "observability.py"
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
    assert summary["total_errors"] == 1
    assert summary["failures"][0]["node"] == "planner"
    assert "boom" in summary["failures"][0]["detail"]


def test_provider_switch_recorded():
    rec = obs.ObservabilityRecorder()
    rec.record_provider_switch("supervisor", "groq", "ollama", "inc-3")
    switches = rec.summary()["provider_switches"]
    assert switches and "groq→ollama" in switches[0]["detail"]


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
    # 1 successful end + 1 error → runs=1, errors=1 → error_rate 1.0
    assert s["nodes"]["n"]["errors"] == 1
    assert s["nodes"]["n"]["runs"] == 1


def test_ring_buffer_caps_events():
    rec = obs.ObservabilityRecorder(max_events=5)
    for _ in range(20):
        rec.record(obs.AgentEvent("n", "start"))
    assert len(rec.events()) == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
