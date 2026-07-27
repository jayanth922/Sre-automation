#!/usr/bin/env python3
"""Unit tests for the resolution report."""

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "resolution_report.py"
_spec = importlib.util.spec_from_file_location("resolution_report", _MODULE_PATH)
rr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rr
_spec.loader.exec_module(rr)


@dataclass
class FakeAlert:
    alert_name: str
    labels: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeReflector:
    hypothesis: str = "A bad deploy raised the error rate."


def _state():
    return {
        "alert_context": FakeAlert("CheckoutHighErrorRate", {"service": "checkout-service"}),
        "reflector_analysis": FakeReflector(),
    }


def test_report_includes_issue_root_cause_and_actions():
    act_report = {
        "severity": "SEV2",
        "aggregate_decision": "autonomous",
        "executed": [{"action_type": "rollback", "target": "checkout-service", "command": "kubectl rollout undo ..."}],
    }
    verification = {"status": "RESOLVED", "detail": "current 0.01 < threshold 0.05"}
    report = rr.build_resolution_report(_state(), act_report, verification=verification)
    md = report["markdown"]
    assert "CheckoutHighErrorRate" in md
    assert "bad deploy" in md.lower()
    assert "rollback" in md
    assert "✅ RESOLVED" in md
    assert report["resolved"] is True


def test_report_includes_sandbox_tested_code_fix():
    act_report = {"severity": "SEV2", "aggregate_decision": "autonomous", "executed": []}
    code_fix = {"status": "TESTED_PASS", "diff": "--- a/app.py\n+++ b/app.py\n-bug\n+fix"}
    report = rr.build_resolution_report(_state(), act_report, verification={"status": "RESOLVED", "detail": ""}, code_fix=code_fix)
    md = report["markdown"]
    assert "Suggested code fix" in md
    assert "sandbox-tested ✅ PASS" in md
    assert "apply on your side" in md.lower()
    assert "```diff" in md


def test_report_high_severity_held_for_approval():
    act_report = {"severity": "SEV1", "aggregate_decision": "requires_approval", "executed": []}
    report = rr.build_resolution_report(_state(), act_report, verification=None)
    assert "Held for human approval" in report["markdown"]
    assert report["resolved"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
