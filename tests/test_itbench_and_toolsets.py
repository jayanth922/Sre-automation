#!/usr/bin/env python3
"""Tests for the ITBench adapter (#1) and the toolset registry (#4)."""

import asyncio
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


itb = _load(_ROOT / "benchmarks" / "itbench_adapter.py", "itbench_adapter")
ts = _load(_ROOT / "sre_agent" / "toolsets.py", "toolsets")


@dataclass
class FakeAlert:
    labels: Dict[str, Any] = field(default_factory=dict)


# ── ITBench adapter ───────────────────────────────────────────────────────────
def test_to_itbench_result_maps_diagnosis_and_remediation():
    act_report = {
        "severity_rationale": "impact×urgency",
        "resolution_report": {"root_cause": "bad deploy to checkout-service"},
        "action_reports": [{"action_type": "rollback", "target": "checkout-service"}],
        "executed": [{"action_type": "rollback", "target": "checkout-service"}],
        "verification": {"status": "RESOLVED"},
    }
    res = itb.to_itbench_result(act_report, FakeAlert({"service": "checkout-service"}))
    assert "bad deploy" in res.fault_hypothesis
    assert "checkout-service" in res.affected_entities
    assert res.remediation[0]["action_type"] == "rollback"
    assert res.resolved is True


def test_to_itbench_result_unresolved_when_no_verification():
    res = itb.to_itbench_result({"action_reports": []}, FakeAlert({"service": "svc"}))
    assert res.resolved is False
    assert res.affected_entities == ["svc"]


def test_run_scenario_uses_injected_invoke():
    async def scenario():
        async def agent_invoke(sc):
            return {
                "act_report": {"resolution_report": {"root_cause": "OOM"},
                               "executed": [{"action_type": "patch", "target": "inv"}],
                               "verification": {"status": "RESOLVED"}},
                "alert": FakeAlert({"service": "inv"}),
                "summary": "patched memory limit",
            }
        return await itb.run_scenario({"fault": "oom"}, agent_invoke)

    res = asyncio.run(scenario())
    assert res.resolved and res.fault_hypothesis == "OOM"
    assert res.diagnosis == "patched memory limit"


# ── toolset registry ──────────────────────────────────────────────────────────
def test_registry_lists_integrated_and_candidates():
    names = {t.name for t in ts.integrated()}
    assert {"kubernetes", "prometheus", "loki", "k8s_executor", "github_exec"} <= names
    cand = {t.name for t in ts.candidates()}
    assert "distributed_tracing" in cand and "datadog" in cand


def test_coverage_counts_add_up():
    cov = ts.coverage()
    assert cov["integrated"] + cov["candidates"] == cov["total"]
    assert cov["integrated"] >= 7


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
