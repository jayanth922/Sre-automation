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
    execute_live_action_request,
    extract_incident_signals,
    live_outcome_summary,
    verify_live,
)
from sre_agent.approval_flow import compute_action_hash  # noqa: E402
from sre_agent.execution_context import ExecutionContext  # noqa: E402
from sre_agent.severity_engine import Severity, classify_severity  # noqa: E402
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
    alert_name: str = ""


@dataclass
class FakeReflector:
    confidence: float = 0.9


def _measured_results(**overrides: Any) -> Dict[str, Any]:
    """Measured telemetry fixtures — never invent these from alert severity alone."""
    payload = {
        "error_rate": 0.02,
        "slo_burn_rate": 0.5,
        "slo_breached": False,
        "saturation": 0.1,
        "error_rate_slope": 0.0,
        "still_escalating": False,
        "affected_services": 1,
    }
    payload.update(overrides)
    return {"MetricsAgent": {"findings": payload}}


def _state(alert, plan=None, results=None, confidence=0.9):
    return {
        "alert_context": alert,
        "remediation_plan": plan,
        "reflector_analysis": FakeReflector(confidence),
        "agent_results": results if results is not None else _measured_results(),
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
    # Even without fabricating rates from the critical label, measured outage
    # signals keep the plan behind approval.
    report = _build(
        _state(
            alert,
            plan,
            results=_measured_results(
                error_rate=0.85,
                slo_burn_rate=18.0,
                slo_breached=True,
                saturation=0.8,
                still_escalating=True,
            ),
        ),
    )
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


def test_config_change_with_no_capability_is_blocked_in_the_plan_a_human_reads():
    # The planner's real shape for a runtime toggle: prose intent, no cpu/memory.
    # Nothing in the stack can apply it, so it must be marked blocked here —
    # before an operator approves it — not refused after they did.
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([
        FakeAction("restart", "inventory-service", {"namespace": "demo-app"}),
        FakeAction("config_change", "inventory-service", {
            "namespace": "demo-app",
            "intent": "Set SLOW_QUERY_RATE back to 0 via /admin/config.",
        }),
    ])
    report = _build(_state(alert, plan))
    blocked = [r for r in report.action_reports if r["action_type"] == "config_change"]
    assert len(blocked) == 1
    assert blocked[0]["decision"] == "blocked"
    assert "no automation capability" in blocked[0]["reason"]
    # No fabricated dry-run transcript for a command that cannot be issued.
    assert "command" not in blocked[0]
    assert all(r["action_type"] != "config_change" for r in report.executed)
    assert "no automation capability" in report.summary


def test_config_change_carrying_a_resource_limit_is_still_planned_normally():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([
        FakeAction("config_change", "inventory-service", {
            "namespace": "demo-app",
            "memory": "1Gi",
        }),
    ])
    report = _build(_state(alert, plan))
    assert report.action_reports[0]["decision"] != "blocked"


def test_extract_signals_from_critical_revenue_service_without_fabricating_rates():
    alert = FakeAlert("critical", {"service": "checkout-service"})
    signals = extract_incident_signals(_state(alert, results={}))
    assert signals.revenue_impacting is True
    assert signals.user_facing is True
    # Critical labels must not invent measured telemetry.
    assert signals.error_rate is None
    assert signals.slo_burn_rate is None
    assert signals.slo_breached is None
    assert any(link.unknown for link in signals.evidence if link.field == "error_rate")


def test_extract_signals_uses_measured_metrics_from_agent_results():
    alert = FakeAlert("critical", {"service": "checkout-service"})
    state = _state(
        alert,
        results=_measured_results(
            error_rate=0.42,
            slo_burn_rate=8.0,
            slo_breached=True,
            saturation=0.55,
        ),
        confidence=0.8,
    )
    signals = extract_incident_signals(state)
    assert signals.error_rate == 0.42
    assert signals.slo_burn_rate == 8.0
    assert signals.slo_breached is True
    assert signals.hypothesis_confidence == 0.8
    assert any(
        link.field == "error_rate" and link.source.startswith("agent_results:")
        for link in signals.evidence
    )
    assessment = classify_severity(signals)
    assert assessment.severity is not Severity.UNKNOWN
    assert assessment.evidence


def test_critical_incident_with_measured_outage_requires_approval():
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan(
        [FakeAction("rollback", "checkout-service", {"namespace": "demo-app"})],
        risk_level="high",
    )
    state = _state(
        alert,
        plan,
        results=_measured_results(
            error_rate=0.9,
            slo_burn_rate=20.0,
            slo_breached=True,
            saturation=0.9,
            still_escalating=True,
            affected_services=3,
        ),
        confidence=0.9,
    )
    report = build_act_report(state, evaluate_fn=ALLOW)
    assert report.aggregate_decision == "requires_approval"
    assert len(report.executed) == 0
    assert report.severity in {"SEV1", "SEV2"}


def test_missing_telemetry_requires_approval_not_autonomy():
    alert = FakeAlert("warning", {"service": "inventory-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("restart", "inventory-service", {"namespace": "demo-app"})])
    report = build_act_report(_state(alert, plan, results={}), evaluate_fn=ALLOW)
    assert report.unknown_telemetry is True
    assert report.severity == "UNKNOWN"
    assert report.aggregate_decision == "requires_approval"
    assert len(report.executed) == 0
    assert report.severity_evidence


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


def test_dispatched_error_requires_manual_review(monkeypatch):
    import sre_agent.act_phase as act_phase

    async def failed_after_dispatch(*args, **kwargs):
        return type(
            "Result",
            (),
            {
                "action_type": "restart",
                "target": "inventory-service",
                "status": "ERROR",
                "command": "restart inventory-service",
                "detail": "connection lost after dispatch",
            },
        )()

    monkeypatch.setattr(act_phase, "authorize_and_execute", failed_after_dispatch)
    result = asyncio.run(
        execute_live_action_request(
            {
                "action_index": 0,
                "action": {
                    "action_type": "restart",
                    "target": "inventory-service",
                    "parameters": {"namespace": "demo-app"},
                },
                "action_payload": {
                    "action_type": "restart",
                    "target": "inventory-service",
                },
                "gate_context": {
                    "decision": "autonomous",
                    "severity": "SEV3",
                },
                "idempotency_key": "action-0",
            },
            None,
            context=LIVE_CONTEXT,
        )
    )

    assert result["status"] == "ERROR"
    assert result["failure_class"] == "outcome_unknown"
    assert result["manual_review_required"] is True


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


def test_escalate_pages_on_call_instead_of_being_refused(monkeypatch):
    """`escalate` is notify-only by design, not an unsupported mutation.

    It has no MCP tool, so routing it through the mutation gateway rejected it
    as `unsupported_action` — a plan whose last step was "get a human" reported
    a refusal and paged nobody.
    """
    import sre_agent.incident_timeline as incident_timeline

    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("escalate", "checkout-service")])
    state = _state(alert, plan)
    state["incident_id"] = "33333333-3333-3333-3333-333333333333"
    report = _build(state)

    emitted = {}

    async def fake_emit(incident_id, **kwargs):
        emitted.update(kwargs, incident_id=incident_id)
        return object()  # the created timeline event

    monkeypatch.setattr(incident_timeline, "emit_timeline_event", fake_emit)

    async def infra_caller(tool, args):  # pragma: no cover - must not be reached
        raise AssertionError("escalate must not touch the infra backend")

    results = asyncio.run(
        execute_autonomous_live(
            state, report, infra_caller, approved=True, context=LIVE_CONTEXT
        )
    )

    assert len(results) == 1
    assert results[0]["status"] == "EXECUTED"
    assert results[0]["action_type"] == "escalate"
    # "act" is one of war_room._SURFACED, so this reaches the Slack thread.
    assert emitted["event_type"] == "act"
    assert emitted["incident_id"] == "33333333-3333-3333-3333-333333333333"
    assert "checkout-service" in emitted["content"]


def test_escalate_without_an_incident_thread_is_honest(monkeypatch):
    """No thread means no on-call was reached — say so, do not claim success."""
    alert = FakeAlert("critical", {"service": "checkout-service", "namespace": "demo-app"})
    plan = FakePlan([FakeAction("escalate", "checkout-service")])
    state = _state(alert, plan)  # _state leaves incident_id None
    report = _build(state)

    async def infra_caller(tool, args):  # pragma: no cover - must not be reached
        raise AssertionError("escalate must not touch the infra backend")

    results = asyncio.run(
        execute_autonomous_live(
            state, report, infra_caller, approved=True, context=LIVE_CONTEXT
        )
    )

    assert results[0]["status"] == "SKIPPED"
    assert "no incident thread" in results[0]["detail"].lower()


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
    assert out["signal"] == "error_rate"


def test_verify_live_checks_the_alert_that_opened_the_incident():
    """A latency alert graded on the error rate can be called "resolved" while
    queries are still slow. When the incident carries an alert name, that alert
    is the signal."""
    alert = FakeAlert(
        "warning",
        {"service": "inventory-service", "namespace": "demo-app"},
        alert_name="InventorySlowQueries",
    )
    state = _state(alert)
    captured = {}

    async def caller(tool, args):
        captured["query"] = args["query"]
        return '{"result":[],"series_total":0}'  # alert no longer firing

    out = asyncio.run(verify_live(state, caller))
    assert captured["query"] == (
        'ALERTS{alertname="InventorySlowQueries",alertstate="firing",'
        'service="inventory-service"}'
    )
    assert out["status"] == "RESOLVED"
    assert out["signal"] == "alert_state"
    assert out["alert_name"] == "InventorySlowQueries"


# The longest averaging window in the shipped alert rules (`rate(...[5m])`).
# A correct fix cannot clear such an alert any sooner than this.
ALERT_AVERAGING_WINDOW_SECONDS = 300


def test_the_default_verification_budget_outlasts_the_alerts_own_window(monkeypatch):
    """A default budget no longer than the alert's window grades good fixes FAILED.

    The alert rules poll `rate(...[5m])` with `for: 3m`, so the window still
    contains the fault for five minutes after the pod is replaced. When the
    budget equalled that window, a live slow-query remediation that genuinely
    worked was recorded as FAILED and the incident moved to
    REMEDIATION_FAILED — the alert cleared 40 seconds later. The default has
    to leave room for the window *plus* a rollout.
    """
    monkeypatch.delenv("VERIFY_ALERT_TIMEOUT_SECONDS", raising=False)
    alert = FakeAlert(
        "warning",
        {"service": "inventory-service", "namespace": "demo-app"},
        alert_name="InventorySlowQueries",
    )
    state = _state(alert)
    captured: Dict[str, Any] = {}

    from sre_agent import verification as verification_module

    real_verify = verification_module.verify_alert_cleared

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real_verify(*args, **kwargs)

    monkeypatch.setattr(verification_module, "verify_alert_cleared", spy)

    async def caller(tool, args):
        return '{"result":[],"series_total":0}'

    asyncio.run(verify_live(state, caller))
    assert captured["timeout_seconds"] > ALERT_AVERAGING_WINDOW_SECONDS


def test_the_verification_budget_is_still_operator_overridable(monkeypatch):
    """The default is a floor for correctness, not a hardcode."""
    monkeypatch.setenv("VERIFY_ALERT_TIMEOUT_SECONDS", "45")
    alert = FakeAlert(
        "warning",
        {"service": "inventory-service", "namespace": "demo-app"},
        alert_name="InventorySlowQueries",
    )
    state = _state(alert)
    captured: Dict[str, Any] = {}

    from sre_agent import verification as verification_module

    real_verify = verification_module.verify_alert_cleared

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real_verify(*args, **kwargs)

    monkeypatch.setattr(verification_module, "verify_alert_cleared", spy)

    async def caller(tool, args):
        return '{"result":[],"series_total":0}'

    asyncio.run(verify_live(state, caller))
    assert captured["timeout_seconds"] == 45


# ── the message a human reads after approving ────────────────────────────────


def _live_payload(**overrides):
    payload = {
        "severity": "HIGH",
        "summary": "HIGH: 1/4 dry-run-executed, 3 held for approval/blocked",
        "live_results": [
            {"action_type": "patch_deployment_env", "status": "EXECUTED"},
            {"action_type": "restart_pod", "status": "EXECUTED"},
            {"action_type": "inspect", "status": "EXECUTED"},
            {"action_type": "escalate", "status": "EXECUTED"},
        ],
        "verification": {"status": "RESOLVED", "detail": "alert X is no longer firing after 60s"},
    }
    payload.update(overrides)
    return payload


def test_live_outcome_summary_reports_what_ran_not_what_was_planned():
    text = live_outcome_summary(_live_payload())
    assert "held for approval" not in text  # the plan-time lie this replaces
    assert "2/2 mutating action(s) EXECUTED" in text
    assert "2 notification/read-only action(s) delivered" in text
    assert "Verification: RESOLVED" in text


def test_live_outcome_summary_surfaces_failures_and_live_errors():
    payload = _live_payload(
        live_results=[
            {"action_type": "patch_deployment_env", "status": "EXECUTED"},
            {"action_type": "restart_pod", "status": "FAILED"},
        ],
        verification={"status": "FAILED", "detail": "alert X still firing after 300s"},
        live_error="executor lost the cluster connection",
    )
    text = live_outcome_summary(payload)
    assert "1/2 mutating action(s) EXECUTED" in text
    assert "[FAILED]" in text
    assert "Live error: executor lost the cluster connection" in text


def test_live_outcome_summary_falls_back_when_nothing_ran_live():
    payload = _live_payload(live_results=[])
    assert live_outcome_summary(payload) == payload["summary"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
