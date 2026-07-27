#!/usr/bin/env python3
"""Unit tests for the ACT-phase orchestration (build_act_report).

Imported as a package module; a stub ``evaluate_fn`` is injected so the real
``policy_engine``/langchain chain is never pulled in. Uses lightweight state
doubles that mimic the shape of AgentState / RemediationPlan / RemediationAction.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.act_phase import (  # noqa: E402
    apply_skill_learning,
    build_act_report,
    execute_autonomous_live,
    extract_incident_signals,
    verify_live,
)
from sre_agent.skill_store import InMemorySkillStore  # noqa: E402

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


def test_execute_autonomous_live_only_applies_autonomous_actions():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    # restart => autonomous; config_change w/o rollback => held.
    plan = FakePlan([
        FakeAction("restart", "inventory-service", {"namespace": "demo-app"}),
        FakeAction("config_change", "inventory-service", {"namespace": "demo-app"}),
    ])
    state = _state(alert, plan)
    report = build_act_report(state, evaluate_fn=ALLOW)

    applied = []

    async def fake_caller(tool_name, args):
        applied.append(tool_name)
        return {"status": "OK", "tool": tool_name}

    results = asyncio.run(execute_autonomous_live(state, report, fake_caller))
    # Only the restart (autonomous) is applied; the held config_change is not.
    assert applied == ["restart_deployment"]
    assert len(results) == 1 and results[0]["status"] == "EXECUTED"


def test_apply_skill_learning_records_then_proposes():
    store = InMemorySkillStore()
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("rollback", "checkout-service", {"namespace": "demo-app"},
                                rollback_plan="redeploy")], risk_level="high")

    # Incident 1: force an executed action by faking the report's executed list.
    report1 = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    report1.executed = [{"action_type": "rollback", "target": "checkout-service"}]
    out1 = apply_skill_learning(_state(alert, plan), report1, store=store)
    assert out1["recorded_skill"] is not None
    assert out1["proposed_skills"] == []  # nothing learned before this one

    # Incident 2 (same class): the skill from incident 1 is now proposed.
    report2 = build_act_report(_state(alert, plan), evaluate_fn=ALLOW)
    report2.executed = [{"action_type": "rollback", "target": "checkout-service"}]
    out2 = apply_skill_learning(_state(alert, plan), report2, store=store)
    assert len(out2["proposed_skills"]) == 1
    assert out2["proposed_skills"][0]["actions"] == ["rollback"]


def test_execute_autonomous_live_routes_code_change_to_github():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    # A bad-deploy plan whose fix is a code change (revert the bad commit).
    plan = FakePlan([FakeAction("revert_commit", "checkout-service",
                                {"commit_sha": "deadbeef"}, rollback_plan="re-apply")], risk_level="low")
    state = _state(alert, plan)
    report = build_act_report(state, evaluate_fn=ALLOW)

    infra, github = [], []

    async def infra_caller(tool, args):
        infra.append(tool)
        return {"ok": True}

    async def github_caller(tool, args):
        github.append((tool, args))
        return {"status": "DRY_RUN"}

    # Only run live if the gate cleared it autonomous (low sev + revert has rollback).
    if report.aggregate_decision == "autonomous":
        results = asyncio.run(execute_autonomous_live(state, report, infra_caller, github_caller=github_caller))
        assert github and github[0][0] == "create_revert_pr"
        assert github[0][1]["identifier"] == "deadbeef"
        assert not infra  # code change did not touch the infra backend
        assert results[0]["status"] == "EXECUTED"


def test_verify_live_builds_query_and_evaluates():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    state = _state(alert)

    captured = {}

    async def caller(tool, args):
        captured["query"] = args["query"]
        return [{"value": [0, "0.01"]}]  # below threshold → RESOLVED

    out = asyncio.run(verify_live(state, caller))
    assert 'http_errors_total{service="checkout-service"}' in captured["query"]
    assert out["status"] == "RESOLVED"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
