"""Alert → ACT gate → status oracle lifecycle."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

for k, v in {
    "POSTGRES_USER": "x",
    "POSTGRES_PASSWORD": "x",
    "POSTGRES_DB": "x",
    "POSTGRES_HOST": "localhost",
    "LLM_PROVIDER": "ollama",
}.items():
    os.environ.setdefault(k, v)

try:
    from backend.models import IncidentStatus
    from sre_agent.agent_state import AlertContext, RemediationAction, RemediationPlan
    from sre_agent.graph_builder import _act_gate_node
    from sre_agent.incident_status import compute_incident_status
except Exception as exc:  # pragma: no cover
    pytest.skip(f"runtime stack unavailable: {exc}", allow_module_level=True)


def _plan(action_type: str, target: str, *, risk: str = "low", rollback=None):
    return RemediationPlan(
        plan_id="p-lifecycle",
        hypothesis="integration lifecycle",
        actions=[
            RemediationAction(
                action_type=action_type,
                target=target,
                parameters={"namespace": "demo-app"},
                safety_check="ok",
                rollback_plan=rollback,
            )
        ],
        estimated_duration="2m",
        risk_level=risk,
        requires_approval=(risk == "high"),
        verification_metrics=["error_rate"],
    )


def _state(plan, alert):
    return {
        "alert_context": alert,
        "remediation_plan": plan,
        "agent_results": {"metrics_agent": "ok"},
        "reflector_analysis": None,
        "incident_id": None,
        "metadata": {},
    }


@pytest.mark.integration
def test_alert_to_oracle_autonomous_path_can_resolve():
    alert = AlertContext(
        alert_name="InventorySlowQueries",
        severity="warning",
        labels={"service": "inventory-service", "namespace": "demo-app"},
        annotations={},
    )
    report = asyncio.run(
        _act_gate_node(_state(_plan("restart", "inventory-service"), alert))
    )["metadata"]["act_report"]

    assert report["aggregate_decision"] == "autonomous"
    assert report["executed"]

    payload = dict(report)
    payload.setdefault("plan_present", True)
    status = compute_incident_status(
        {},
        payload,
        SimpleNamespace(status="RESOLVED"),
    )
    assert status == IncidentStatus.RESOLVED


@pytest.mark.integration
def test_critical_alert_maps_to_awaiting_approval():
    alert = AlertContext(
        alert_name="CheckoutHighErrorRate",
        severity="critical",
        labels={"service": "checkout-service", "namespace": "demo-app"},
        annotations={},
    )
    plan = _plan("rollback", "checkout-service", risk="high", rollback="redeploy")
    report = asyncio.run(_act_gate_node(_state(plan, alert)))["metadata"]["act_report"]

    assert report["aggregate_decision"] == "blocked"
    assert report["executed"] == []

    payload = dict(report)
    payload.setdefault("plan_present", True)
    status = compute_incident_status({}, payload, None)
    assert status == IncidentStatus.AWAITING_APPROVAL
