#!/usr/bin/env python3
"""Unit tests for the ACT-phase orchestration (build_act_report).

Imported as a package module; a stub ``evaluate_fn`` is injected so the real
``policy_engine``/langchain chain is never pulled in. Uses lightweight state
doubles that mimic the shape of AgentState / RemediationPlan / RemediationAction.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.act_phase import build_act_report, extract_incident_signals  # noqa: E402

ALLOW = lambda a, e, r: (True, "allowed")  # noqa: E731


@dataclass
class FakeAction:
    action_type: str
    target: str = "inventory-service"
    parameters: Dict[str, Any] = field(default_factory=dict)
    rollback_plan: Optional[str] = None


@dataclass
class FakePlan:
    actions: List[FakeAction]
    risk_level: str = "low"


@dataclass
class FakeAlert:
    severity: str
    labels: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeReflector:
    confidence: float = 0.9


def _state(alert, plan=None, results=None, confidence=0.9):
    return {
        "alert_context": alert,
        "remediation_plan": plan,
        "reflector_analysis": FakeReflector(confidence),
        "agent_results": results or {"metrics_agent": "ok"},
        "incident_id": None,
        "metadata": {},
    }


def test_no_plan_skips_act():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    report = build_act_report(_state(alert, plan=None), evaluate_fn=ALLOW)
    assert report.plan_present is False
    assert report.aggregate_decision is None
    assert "no remediation plan" in report.summary.lower()


def test_low_severity_reversible_is_autonomously_dry_run():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("restart", "inventory-service", {"namespace": "demo-app"})])
    report = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    assert report.plan_present is True
    assert report.aggregate_decision == "autonomous"
    assert len(report.executed) == 1
    assert report.executed[0]["command"].startswith("kubectl rollout restart")
    assert report.executed[0]["audit_hash"]


def test_critical_incident_requires_approval():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("rollback", "checkout-service", {"namespace": "demo-app"})],
                    risk_level="high")
    report = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    assert report.aggregate_decision == "requires_approval"
    assert len(report.executed) == 0


def test_mixed_plan_executes_autonomous_holds_the_rest():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([
        FakeAction("restart", "inventory-service", {"namespace": "demo-app"}),
        FakeAction("config_change", "inventory-service", {"namespace": "demo-app"}),  # no rollback
    ])
    report = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    # One autonomous (restart), one held (config_change w/o rollback) → plan needs approval.
    assert report.aggregate_decision == "requires_approval"
    assert len(report.executed) == 1
    assert len(report.action_reports) == 2


def test_extract_signals_from_critical_revenue_service():
    alert = FakeAlert("critical", {"service": "checkout-service"})
    signals = extract_incident_signals(_state(alert))
    assert signals.revenue_impacting is True
    assert signals.user_facing is True
    assert signals.slo_breached is True


def test_report_is_serializable():
    alert = FakeAlert("warning", {"service": "inventory-service"})
    plan = FakePlan([FakeAction("restart")])
    report = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    d = report.to_dict()
    assert isinstance(d, dict) and d["plan_present"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
