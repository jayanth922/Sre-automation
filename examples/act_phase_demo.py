#!/usr/bin/env python3
"""
ACT-phase end-to-end demo (Phase 0, dry-run) — no cluster required.

Runs the full decision path for several incident scenarios:

    IncidentSignals → SeverityEngine → PolicyGate → Executor(dry-run)

and prints the severity classification, the autonomy decision, and the exact
command the agent *would* run (plus a tamper-evident audit hash). Nothing here
touches real infrastructure.

Run:
    cd /path/to/SRE_Agent_Intermediate
    python examples/act_phase_demo.py
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.executor import Executor
from sre_agent.policy_gate import AutonomyDecision, decide
from sre_agent.severity_engine import IncidentSignals, classify_severity


@dataclass
class RemediationAction:
    """Minimal stand-in matching the real pydantic RemediationAction shape."""

    action_type: str
    target: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    safety_check: str = ""
    rollback_plan: Optional[str] = None


# In the live graph the Policy Gate calls sre_agent.policy_engine.evaluate_action.
# Here we inline a tiny version so the demo runs with zero dependencies.
def demo_policy(action: Any, environment: str, risk_score: float):
    at = action.action_type.lower()
    params = action.parameters or {}
    if at == "scale" and params.get("replicas") == 0 and environment == "production":
        return False, "scale-to-0 forbidden in production"
    if at == "restart" and environment == "production" and risk_score >= 8:
        return False, f"restart blocked in prod at risk {risk_score}"
    return True, "allowed"


SCENARIOS = [
    {
        "name": "Single-pod CrashLoop in dev (minor)",
        "environment": "dev",
        "signals": IncidentSignals(
            affected_services=1, error_rate=0.03, slo_breached=False,
            slo_burn_rate=0.4, saturation=0.1, hypothesis_confidence=0.9,
        ),
        "action": RemediationAction("restart", "inventory-service",
                                    parameters={"namespace": "demo-app"}),
    },
    {
        "name": "Checkout total outage in prod (critical)",
        "environment": "production",
        "signals": IncidentSignals(
            affected_services=4, user_facing=True, revenue_impacting=True,
            error_rate=0.92, slo_breached=True, slo_burn_rate=22.0,
            saturation=0.85, still_escalating=True, hypothesis_confidence=0.9,
        ),
        "action": RemediationAction("rollback", "checkout-service",
                                    parameters={"namespace": "demo-app"},
                                    rollback_plan="redeploy previous revision"),
    },
    {
        "name": "Memory pressure, low sev, with rollback plan",
        "environment": "production",
        "signals": IncidentSignals(
            affected_services=1, error_rate=0.05, slo_breached=False,
            slo_burn_rate=1.5, saturation=0.55, hypothesis_confidence=0.85,
        ),
        "action": RemediationAction("config_change", "inventory-service",
                                    parameters={"namespace": "demo-app"},
                                    rollback_plan="kubectl apply previous configmap"),
    },
    {
        "name": "Proposed scale-to-0 (irreversible) at low sev",
        "environment": "production",
        "signals": IncidentSignals(
            affected_services=1, error_rate=0.04, slo_burn_rate=0.5,
            hypothesis_confidence=0.9,
        ),
        "action": RemediationAction("scale", "checkout-service",
                                    parameters={"replicas": 0, "namespace": "demo-app"}),
    },
    {
        "name": "Low confidence bumps severity up (safety)",
        "environment": "production",
        "signals": IncidentSignals(
            affected_services=2, error_rate=0.3, slo_burn_rate=5.0,
            saturation=0.4, hypothesis_confidence=0.2,  # unsure → round up
        ),
        "action": RemediationAction("restart", "checkout-service",
                                    parameters={"namespace": "demo-app"}),
    },
]

_ICON = {
    AutonomyDecision.AUTONOMOUS: "🟢 AUTONOMOUS",
    AutonomyDecision.REQUIRES_APPROVAL: "🟡 REQUIRES APPROVAL",
    AutonomyDecision.BLOCKED: "🔴 BLOCKED",
}


def main() -> None:
    print("=" * 74)
    print("  ACT-phase decision path — Phase 0 dry-run (no cluster touched)")
    print("=" * 74)

    executor = Executor(actor="sre-agent", incident_id="demo")

    for i, sc in enumerate(SCENARIOS, 1):
        assessment = classify_severity(sc["signals"])
        gate = decide(
            sc["action"], assessment,
            environment=sc["environment"],
            risk_score=6.0,
            evaluate_fn=demo_policy,
        )

        print(f"\n[{i}] {sc['name']}  (env={sc['environment']})")
        print(f"    Severity : {assessment.severity.name}  ({assessment.rationale})")
        print(f"    Action   : {sc['action'].action_type} → {sc['action'].target}")
        print(f"    Gate     : {_ICON[gate.decision]}  — {gate.reason}")

        if gate.decision is AutonomyDecision.AUTONOMOUS:
            result = executor.execute(sc["action"], gate.decision.value, dry_run=True)
            print(f"    Execute  : would run → {result.command}")
            print(f"    Audit    : {result.audit['content_hash'][:16]}…  (sha256, tamper-evident)")
            if result.rollback_command:
                print(f"    Rollback : {result.rollback_command}")
        else:
            print("    Execute  : (held — routed to human checkpoint, no action taken)")

    print("\n" + "=" * 74)
    print("  Summary: severity drives autonomy; reversibility floors it; every")
    print("  autonomous action is dry-run + audited. Zero production risk.")
    print("=" * 74)


if __name__ == "__main__":
    main()
