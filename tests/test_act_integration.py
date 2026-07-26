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

import pytest

# Backend import chain needs these present to build the engine at import time.
for k, v in {
    "POSTGRES_USER": "x", "POSTGRES_PASSWORD": "x", "POSTGRES_DB": "x",
    "POSTGRES_HOST": "localhost", "LLM_PROVIDER": "ollama",
}.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from sre_agent.agent_state import AlertContext, RemediationAction, RemediationPlan
    from sre_agent.graph_builder import _act_gate_node
except Exception as exc:  # pragma: no cover - env-dependent
    pytest.skip(f"full runtime stack unavailable: {exc}", allow_module_level=True)


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


def test_low_severity_reversible_runs_autonomously_and_records_skill():
    alert = AlertContext(alert_name="InventorySlowQueries", severity="warning",
                         labels={"service": "inventory-service", "namespace": "demo-app"}, annotations={})
    report = asyncio.run(_act_gate_node(_state(_plan("restart", "inventory-service"), alert)))["metadata"]["act_report"]
    assert report["aggregate_decision"] == "autonomous"
    assert len(report["executed"]) == 1
    assert report["executed"][0]["command"].startswith("kubectl rollout restart")
    assert report["recorded_skill"] is not None  # self-improving loop fired


def test_critical_incident_requires_approval_and_does_not_execute():
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
