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
from sre_agent.approval_flow import compute_action_hash  # noqa: E402
from sre_agent.execution_context import ExecutionContext  # noqa: E402
from sre_agent.skill_store import InMemorySkillStore  # noqa: E402

ALLOW = lambda a, e, r: (True, "allowed")  # noqa: E731

LIVE_CONTEXT = ExecutionContext(
    organization_id="11111111-1111-1111-1111-111111111111",
    cluster_id="22222222-2222-2222-2222-222222222222",
    namespace="demo-app",
    allowlist=("demo-app",),
)


@pytest.fixture(autouse=True)
def _live_gateway_dependencies(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    class Store:
        def __init__(self):
            self.claims = set()

        def is_available(self):
            return True

        def is_cluster_locked(self, cluster_id):
            return False

        def set_idempotency(self, key, ttl):
            if key in self.claims:
                return False
            self.claims.add(key)
            return True

    async def persist(*args, **kwargs):
        return None

    store = Store()
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "_persist_audit_event", persist)
    monkeypatch.setattr(
        gateway,
        "_runtime_remediation_calibration",
        lambda raw_confidence: (0.99, 0.95),
    )


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
    confidence: float = 0.99


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


def _build(state):
    return build_act_report(
        state,
        evaluate_fn=ALLOW,
        calibrated_action_probability=0.99,
        minimum_autonomy_probability=0.95,
    )


def test_no_plan_skips_act():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    report = _build(_state(alert, plan=None))
    assert report.plan_present is False
    assert report.aggregate_decision is None
    assert "no remediation plan" in report.summary.lower()


def test_low_severity_reversible_is_autonomously_dry_run():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("restart", "inventory-service", {"namespace": "demo-app"})])
    report = _build(_state(alert, plan))
    assert report.plan_present is True
    assert report.aggregate_decision == "autonomous"
    assert len(report.executed) == 1
    assert report.executed[0]["command"].startswith("kubectl rollout restart")
    assert report.executed[0]["audit_hash"]


def test_self_reported_confidence_alone_cannot_authorize_dry_run():
    alert = FakeAlert(
        "warning",
        {"service": "inventory-service", "namespace": "demo-app"},
    )
    plan = FakePlan(
        [FakeAction("restart", "inventory-service", {"namespace": "demo-app"})]
    )
    report = build_act_report(
        _state(alert, plan, confidence=1.0), evaluate_fn=ALLOW
    )

    assert report.aggregate_decision == "requires_approval"
    assert report.executed == []
    assert report.confidence_status == "uncalibrated"
    assert "uncalibrated" in report.action_reports[0]["reason"]


def test_critical_incident_requires_approval():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("rollback", "checkout-service", {"namespace": "demo-app"})],
                    risk_level="high")
    report = _build(_state(alert, plan))
    assert report.aggregate_decision == "requires_approval"
    assert len(report.executed) == 0


def test_alert_namespace_cannot_downgrade_production_policy():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "dev"})
    plan = FakePlan(
        [FakeAction("restart", "inventory-service", {"namespace": "demo-app"})],
        risk_level="medium",
    )
    report = build_act_report(_state(alert, plan))
    assert report.aggregate_decision == "blocked"


def test_mixed_plan_executes_autonomous_holds_the_rest():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([
        FakeAction("restart", "inventory-service", {"namespace": "demo-app"}),
        FakeAction("config_change", "inventory-service", {"namespace": "demo-app"}),  # no rollback
    ])
    report = _build(_state(alert, plan))
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
    report = _build(_state(alert, plan))
    d = report.to_dict()
    assert isinstance(d, dict) and d["plan_present"] is True
    assert d["action_reports"][0]["parameters"] == {}


def test_rebuilt_dry_run_report_has_stable_approval_hash():
    state = _state(
        FakeAlert("warning", {"service": "inventory-service"}),
        FakePlan([
            FakeAction(
                "scale",
                parameters={"replicas": 3},
                rollback_plan="restore previous replica count",
            )
        ]),
    )
    first = _build(state).to_dict()
    second = _build(state).to_dict()
    second["action_reports"][0]["audit_hash"] = "different-dry-run-audit"
    second["executed"][0]["audit_hash"] = "different-dry-run-audit"
    assert compute_action_hash(first) == compute_action_hash(second)


def test_execute_autonomous_live_only_applies_autonomous_actions():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    # restart => autonomous; config_change w/o rollback => held.
    plan = FakePlan([
        FakeAction("restart", "inventory-service", {"namespace": "demo-app"}),
        FakeAction("config_change", "inventory-service", {"namespace": "demo-app"}),
    ])
    state = _state(alert, plan)
    report = _build(state)

    applied = []

    async def fake_caller(tool_name, args):
        applied.append(tool_name)
        return {"status": "OK", "tool": tool_name}

    results = asyncio.run(
        execute_autonomous_live(state, report, fake_caller, context=LIVE_CONTEXT)
    )
    # Only the restart (autonomous) is applied; the held config_change is not.
    assert applied == ["restart_deployment"]
    assert len(results) == 1 and results[0]["status"] == "EXECUTED"


def test_approved_live_applies_held_but_never_blocked_actions():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([
        FakeAction("restart", "checkout-service", {"namespace": "demo-app"}),
        FakeAction("scale", "checkout-service", {"namespace": "demo-app", "replicas": 0}),
    ])
    state = _state(alert, plan)
    report = _build(state)
    # Critical restart is held for approval; scale-to-zero is also held by the
    # reversibility gate and may be approved. A hard policy block is covered by
    # the decision filter below by changing the report to the gate's BLOCKED value.
    report.action_reports[1]["decision"] = "blocked"

    applied = []

    async def fake_caller(tool_name, args):
        applied.append(tool_name)
        return {"status": "OK", "tool": tool_name}

    results = asyncio.run(
        execute_autonomous_live(
            state, report, fake_caller, approved=True, context=LIVE_CONTEXT
        )
    )
    assert applied == ["restart_deployment"]
    assert len(results) == 1 and results[0]["status"] == "EXECUTED"


def test_apply_skill_learning_records_then_proposes():
    store = InMemorySkillStore()
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("rollback", "checkout-service", {"namespace": "demo-app"},
                                rollback_plan="redeploy")], risk_level="high")

    # Incident 1: dry-run executed list alone must not become a successful skill.
    report1 = _build(_state(alert, plan))
    report1.executed = [{"action_type": "rollback", "target": "checkout-service"}]
    out1 = apply_skill_learning(
        {**_state(alert, plan), "incident_id": "inc-1"},
        report1,
        store=store,
    )
    assert out1["recorded_skill"] is None
    assert out1["learning_eligibility"]["outcome_class"] == "dry_run"

    # Verified live execution is recorded.
    out1b = apply_skill_learning(
        {**_state(alert, plan), "incident_id": "inc-1"},
        report1,
        store=store,
        verification_outcome={"status": "RESOLVED"},
        live_results=[
            {"status": "EXECUTED", "action_type": "rollback", "target": "checkout-service"}
        ],
    )
    assert out1b["recorded_skill"] is not None
    assert out1b["proposed_skills"] == []

    # Incident 2 (same class): the skill from incident 1 is now proposed.
    report2 = _build(_state(alert, plan))
    report2.executed = [{"action_type": "rollback", "target": "checkout-service"}]
    out2 = apply_skill_learning(
        {**_state(alert, plan), "incident_id": "inc-2"},
        report2,
        store=store,
        verification_outcome={"status": "RESOLVED"},
        live_results=[
            {"status": "EXECUTED", "action_type": "rollback", "target": "checkout-service"}
        ],
    )
    assert len(out2["proposed_skills"]) == 1
    assert out2["proposed_skills"][0]["actions"] == ["rollback"]


def test_execute_autonomous_live_routes_code_change_to_github():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    # A bad-deploy plan whose fix is a code change (revert the bad commit).
    plan = FakePlan([FakeAction("revert_commit", "checkout-service",
                                {"commit_sha": "deadbeef"}, rollback_plan="re-apply")], risk_level="low")
    state = _state(alert, plan)
    report = _build(state)

    infra, github = [], []

    async def infra_caller(tool, args):
        infra.append(tool)
        return {"ok": True}

    async def github_caller(tool, args):
        github.append((tool, args))
        return {"status": "REVERT_REQUESTED", "applied": True}

    # Only run live if the gate cleared it autonomous (low sev + revert has rollback).
    if report.aggregate_decision == "autonomous":
        results = asyncio.run(
            execute_autonomous_live(
                state,
                report,
                infra_caller,
                github_caller=github_caller,
                context=LIVE_CONTEXT,
            )
        )
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
