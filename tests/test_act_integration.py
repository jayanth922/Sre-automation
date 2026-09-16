#!/usr/bin/env python3
"""
Integration test for the ACT pipeline.

Runs the real `_act_gate_node` end-to-end over the *real* pydantic models
(AgentState / RemediationPlan / AlertContext) and the real policy engine,
severity engine, policy gate, dry-run executor, and skill store — no mocks
except that live execution is off (dry-run), so no MCP/cluster is needed.

Skips cleanly when the full runtime stack (langchain/langgraph/sqlalchemy) is not
importable, so the dependency-light unit suite still runs everywhere.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Backend import chain needs these present to build the engine at import time.
for k, v in {
    "POSTGRES_USER": "x", "POSTGRES_PASSWORD": "x", "POSTGRES_DB": "x",
    "POSTGRES_HOST": "localhost", "LLM_PROVIDER": "anthropic",
}.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from sre_agent.agent_state import AlertContext, RemediationAction, RemediationPlan
    from sre_agent.graph_builder import (
        _act_gate_node,
        _prepare_approval_node,
        _sandbox_params_ready,
    )
except Exception as exc:  # pragma: no cover - env-dependent
    pytest.skip(f"full runtime stack unavailable: {exc}", allow_module_level=True)


# ── Phase F: shared sandbox-readiness gate (pure, no I/O) ────────────────────
def test_sandbox_params_ready_requires_runner_image_and_failure_signature():
    assert not _sandbox_params_ready("", [], [], "", "sig")
    assert not _sandbox_params_ready("img", [], [], "", "")


def test_sandbox_params_ready_true_with_no_patch_yet():
    # No patch: generate_patch_activity will produce one, so baseline/candidate
    # commands aren't required up front.
    assert _sandbox_params_ready("img", [], [], "", "sig")


def test_sandbox_params_ready_requires_commands_when_patch_present():
    assert not _sandbox_params_ready("img", [], [], "diff", "sig")
    assert not _sandbox_params_ready("img", ["cmd"], [], "diff", "sig")
    assert _sandbox_params_ready("img", ["cmd"], ["cmd"], "diff", "sig")


def test_code_fix_action_deferred_even_when_temporal_disabled(monkeypatch):
    # Phase E cutover (docs/ai/DECISIONS.md): a detected code-fix action must
    # always be deferred from the old single-gate execute_autonomous_live
    # path, never just when the deterministic pipeline happens to be ready to
    # start. Temporal is disabled in the test env (no TEMPORAL_ENABLED set)
    # AND temporalio isn't installed here (it's an optional extra graph_builder.py
    # falls back around — see its ImportError handler) — this is exactly the
    # "worst case" the cutover fix must still hold under: previously it left
    # the action's decision untouched and eligible for old-path live
    # execution. Asserted against the literal sentinel value, not the
    # temporalio-gated import, so this test runs without that optional extra.
    deferred_sentinel = "deferred_to_deterministic_pipeline"

    alert = AlertContext(
        alert_name="CheckoutNilPointer", severity="warning",
        labels={"service": "checkout-service", "namespace": "demo-app"}, annotations={},
    )
    plan = _plan("code_fix", "checkout-service")
    state = _state(plan, alert)
    state["incident_id"] = "inc-test-code-fix"
    report = asyncio.run(_act_gate_node(state))["metadata"]["act_report"]
    assert report["action_reports"][0]["decision"] == deferred_sentinel


def test_resolved_incident_never_creates_an_approval(monkeypatch):
    from sre_agent import approval_flow

    async def resolved(**_kwargs):
        return True

    monkeypatch.setattr(approval_flow, "incident_is_resolved", resolved)
    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={"service": "inventory-service", "namespace": "demo-app"},
        annotations={},
    )
    state = _state(_plan("restart", "inventory-service"), alert)
    state["incident_id"] = "incident-resolved-before-approval"
    context = SimpleNamespace(
        environment="production",
        cluster_id="22222222-2222-2222-2222-222222222222",
    )

    output = asyncio.run(_prepare_approval_node(state, context))

    assert "pending_approval" not in output["metadata"]
    report = output["metadata"]["act_report"]
    assert report["remediation_suppressed"]["reason"] == "incident_resolved"
    assert "no approval or live write" in report["summary"]


def test_resolved_incident_does_not_start_deterministic_code_fix(monkeypatch):
    from sre_agent import approval_flow

    async def resolved(**_kwargs):
        return True

    monkeypatch.setattr(approval_flow, "incident_is_resolved", resolved)
    alert = AlertContext(
        alert_name="CheckoutNilPointer",
        severity="warning",
        labels={"service": "checkout-service", "namespace": "demo-app"},
        annotations={},
    )
    state = _state(_plan("code_fix", "checkout-service"), alert)
    state["incident_id"] = "incident-resolved-before-act"
    context = SimpleNamespace(
        environment="production",
        cluster_id="22222222-2222-2222-2222-222222222222",
    )

    report = asyncio.run(_act_gate_node(state, context))["metadata"]["act_report"]

    assert report["remediation_suppressed"]["reason"] == "incident_resolved"
    assert report["executed"] == []
    assert "code_fix" not in report


def test_human_reopen_clears_checkpointed_suppression(monkeypatch):
    from sre_agent import approval_flow

    async def unresolved(**_kwargs):
        return False

    monkeypatch.setattr(approval_flow, "incident_is_resolved", unresolved)
    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={"service": "inventory-service", "namespace": "demo-app"},
        annotations={},
    )
    state = _state(None, alert)
    state["incident_id"] = "human-reopened-incident"
    state["metadata"] = {
        "remediation_suppressed": {"reason": "incident_resolved"}
    }
    context = SimpleNamespace(
        environment="production",
        cluster_id="22222222-2222-2222-2222-222222222222",
    )

    prepared = asyncio.run(_prepare_approval_node(state, context))
    acted = asyncio.run(_act_gate_node(state, context))

    assert "remediation_suppressed" not in prepared["metadata"]
    assert "remediation_suppressed" not in acted["metadata"]


def test_remediation_action_accepts_code_fix_type():
    # Phase F: the planner can now propose action_type="code_fix" for
    # source-level bugs (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md Phase F).
    action = RemediationAction(
        action_type="code_fix", target="checkout-service",
        parameters={"description": "nil pointer in handler.go"}, safety_check="sandbox-verified",
    )
    assert action.action_type == "code_fix"


@pytest.fixture(autouse=True)
def _isolate_generated_runbooks(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNBOOKS_DIR", str(tmp_path))


def _plan(action_type, target, risk="low", rollback=None):
    return RemediationPlan(
        plan_id="p-int",
        hypothesis="integration test",
        actions=[RemediationAction(action_type=action_type, target=target,
                                   parameters={"namespace": "demo-app"},
                                   safety_check="ok", rollback_plan=rollback)],
        estimated_duration="2m",
        risk_level=risk,
        requires_approval=(risk == "high"),
        verification_metrics=["error_rate"],
    )


def _state(plan, alert):
    return {
        "alert_context": alert, "remediation_plan": plan,
        "agent_results": {"metrics_agent": "ok"}, "reflector_analysis": None,
        "incident_id": None, "metadata": {},
    }


def test_low_severity_reversible_runs_autonomously_and_records_skill(monkeypatch):
    from sre_agent.confidence_calibration import CalibratedConfidence, Task
    import sre_agent.act_phase
    
    def fake_calibrated(raw, task, *args, **kwargs):
        if raw is None:
            raw = 0.9
        return CalibratedConfidence(
            task="diagnosis" if task == "hypothesis" else "remediation",
            raw_confidence=raw,
            calibrated_probability=0.99,
            artifact_version="mock",
            artifact_sha256="mock",
            autonomy_threshold=0.8
        )
        
    def fake_remediation(*args, **kwargs):
        return 0.9, fake_calibrated(0.9, "remediation")
    
    monkeypatch.setattr(sre_agent.act_phase, "_configured_confidence", fake_calibrated)
    monkeypatch.setattr(sre_agent.act_phase, "_configured_remediation_confidence", fake_remediation)
    monkeypatch.setattr(sre_agent.act_phase, "apply_skill_learning", lambda *args, **kwargs: {"recorded_skill": {"name": "mocked"}})

    # R10: severity is evidence-based — supply a complete calm telemetry set so
    # the gate can classify SEV4 without unknown-field escalation.
    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={
            "service": "inventory-service",
            "namespace": "demo-app",
            "error_rate": "0.02",
            "slo_burn_rate": "0.5",
            "saturation": "0.1",
            "affected_services": "1",
            "affected_pods": "1",
            "dependency_count": "0",
            "duration_seconds": "60",
            "customer_scope": "single",
            "slo_breached": "false",
            "still_escalating": "false",
            "error_rate_slope": "0",
        },
        annotations={},
    )
    state = _state(_plan("restart", "inventory-service"), alert)
    state["reflector_analysis"] = {"confidence": 0.9, "confidence_calibrated": True}
    report = asyncio.run(_act_gate_node(state))["metadata"]["act_report"]
    assert report["aggregate_decision"] == "autonomous"
    assert len(report["executed"]) == 1
    assert report["executed"][0]["command"].startswith("kubectl rollout restart")
    assert report["recorded_skill"] is not None  # self-improving loop fired


def test_live_act_hands_the_exact_action_batch_to_temporal(monkeypatch):
    """The graph owns policy; Temporal owns the per-action resume boundary."""
    from sre_agent import act_phase, approval_flow, temporal_client
    from sre_agent.confidence_calibration import CalibratedConfidence

    def fake_calibrated(raw, task, *args, **kwargs):
        return CalibratedConfidence(
            task="diagnosis" if task == "hypothesis" else "remediation",
            raw_confidence=0.9 if raw is None else raw,
            calibrated_probability=0.99,
            artifact_version="mock",
            artifact_sha256="mock",
            autonomy_threshold=0.8,
        )

    async def unresolved(**_kwargs):
        return False

    captured = {}

    async def execute_or_join(workflow, args, *, workflow_id, **_kwargs):
        captured["workflow"] = workflow
        captured["input"] = args[0]
        captured["workflow_id"] = workflow_id
        return {
            "status": "SUPPRESSED_ALERT_CLEARED",
            "live_results": [
                {
                    "action_type": "restart",
                    "target": "inventory-service",
                    "status": "EXECUTED",
                    "command": "restart inventory-service",
                    "detail": "done",
                },
                {
                    "action_type": "restart",
                    "target": "inventory-service",
                    "status": "REFUSED",
                    "command": "",
                    "detail": "incident_resolved: source alert cleared",
                    "rejection_code": "incident_resolved",
                },
            ],
        }

    monkeypatch.setattr(act_phase, "_configured_confidence", fake_calibrated)
    monkeypatch.setattr(
        act_phase,
        "_configured_remediation_confidence",
        lambda *_args, **_kwargs: (0.9, fake_calibrated(0.9, "remediation")),
    )
    monkeypatch.setattr(act_phase, "apply_skill_learning", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(approval_flow, "incident_is_resolved", unresolved)
    monkeypatch.setattr(temporal_client, "temporal_enabled", lambda: True)
    monkeypatch.setattr(temporal_client, "execute_or_join_workflow", execute_or_join)
    monkeypatch.setenv("EXECUTOR_LIVE", "true")

    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={
            "service": "inventory-service",
            "namespace": "demo-app",
            "error_rate": "0.02",
            "slo_burn_rate": "0.5",
            "saturation": "0.1",
            "affected_services": "1",
            "affected_pods": "1",
            "dependency_count": "0",
            "duration_seconds": "60",
            "customer_scope": "single",
            "slo_breached": "false",
            "still_escalating": "false",
            "error_rate_slope": "0",
        },
        annotations={},
    )
    plan = RemediationPlan(
        plan_id="p-live-temporal",
        hypothesis="integration test",
        actions=[
            RemediationAction(
                action_type="restart",
                target="planner prose target",
                parameters={"namespace": "demo-app"},
                safety_check="ok",
            ),
            RemediationAction(
                action_type="restart",
                target="planner prose target",
                parameters={"namespace": "demo-app"},
                safety_check="ok",
            ),
        ],
        estimated_duration="2m",
        risk_level="low",
        requires_approval=False,
        verification_metrics=["error_rate"],
    )
    state = _state(plan, alert)
    state["incident_id"] = "incident-live-temporal"
    state["reflector_analysis"] = {
        "confidence": 0.9,
        "confidence_calibrated": True,
    }
    context = SimpleNamespace(
        organization_id="org-1",
        cluster_id="cluster-1",
        environment="production",
    )

    report = asyncio.run(_act_gate_node(state, context))["metadata"]["act_report"]

    assert captured["workflow_id"].startswith(
        "incident-live-remediation-incident-live-temporal-"
    )
    assert len(captured["input"].action_requests) == 2
    assert captured["input"].action_requests[0]["action"]["target"] == (
        "inventory-service"
    )
    assert report["live_results"][0]["status"] == "EXECUTED"
    assert report["remediation_halted"]["reason"] == "incident_resolved"
    assert "verification" not in report


def test_live_summary_explains_post_clear_verification_skip():
    from sre_agent.act_phase import live_outcome_summary

    summary = live_outcome_summary(
        {
            "severity": "SEV2",
            "live_results": [
                {
                    "action_type": "scale",
                    "status": "EXECUTED",
                    "target": "inventory-service",
                },
                {
                    "action_type": "scale",
                    "status": "REFUSED",
                    "target": "inventory-service",
                    "rejection_code": "incident_resolved",
                },
            ],
            "remediation_halted": {"reason": "incident_resolved"},
        }
    )

    assert "1/2 mutating action(s) EXECUTED" in summary
    assert "Verification: skipped after the source alert cleared" in summary
    assert "nothing mutating executed" not in summary

def test_low_severity_reversible_waits_without_calibration():
    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={
            "service": "inventory-service",
            "namespace": "demo-app",
            "error_rate": "0.02",
            "slo_burn_rate": "0.5",
            "saturation": "0.1",
            "affected_services": "1",
            "affected_pods": "1",
            "dependency_count": "0",
            "duration_seconds": "60",
            "customer_scope": "single",
            "slo_breached": "false",
            "still_escalating": "false",
            "error_rate_slope": "0",
        },
        annotations={},
    )
    report = asyncio.run(_act_gate_node(_state(_plan("restart", "inventory-service"), alert)))["metadata"]["act_report"]
    assert report["aggregate_decision"] == "requires_approval"
    assert report["executed"] == []
    assert report["confidence_status"] == "uncalibrated"
    assert report["recorded_skill"] is None



def test_critical_production_rollback_is_held_for_a_human_not_blocked():
    """Renamed from ..._without_approval_flag_is_blocked.

    The "approval flag" was `parameters["explicit_approval"]`, which no code
    path ever set and only the planner LLM could have set. The old assertion
    pinned a rollback that a human could not authorize and a prompt injection
    could. It must be held, not blocked — and still not executed.
    """
    alert = AlertContext(alert_name="CheckoutHighErrorRate", severity="critical",
                         labels={"service": "checkout-service", "namespace": "demo-app"}, annotations={})
    plan = _plan("rollback", "checkout-service", risk="high", rollback="redeploy")
    report = asyncio.run(_act_gate_node(_state(plan, alert)))["metadata"]["act_report"]
    assert report["aggregate_decision"] == "requires_approval"
    assert len(report["executed"]) == 0


def test_no_plan_skips_act_cleanly():
    alert = AlertContext(alert_name="InventorySlowQueries", severity="warning",
                         labels={"service": "inventory-service"}, annotations={})
    state = {"alert_context": alert, "remediation_plan": None, "agent_results": {},
             "reflector_analysis": None, "incident_id": None, "metadata": {}}
    report = asyncio.run(_act_gate_node(state))["metadata"]["act_report"]
    assert report["plan_present"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
