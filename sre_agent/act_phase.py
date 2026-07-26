#!/usr/bin/env python3
"""
ACT phase orchestration — the pure glue between DECIDE and execution.

``build_act_report(state)`` is the testable core that the LangGraph ``act_gate``
node wraps. Given the graph state (which, when the planner path is active,
contains a ``remediation_plan``), it:

    1. extracts incident signals from the alert + investigation results,
    2. classifies severity (severity_engine),
    3. gates the whole plan (policy_gate),
    4. dry-run-executes the autonomous actions (executor),

and returns a structured, serializable ``ActReport``. It performs no DB or LLM
calls, so it is fully unit-testable; the graph node adds only timeline emission.

Everything here is duck-typed (dict *or* pydantic object) so it works both with
the real ``AgentState`` / ``RemediationPlan`` and with lightweight test doubles.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .executor import Executor
from .policy_gate import AutonomyDecision, decide_plan
from .severity_engine import IncidentSignals, SeverityAssessment, classify_severity

logger = logging.getLogger(__name__)

# Services whose failure is inherently customer- and revenue-facing.
_REVENUE_SERVICES = {"checkout", "checkout-service", "payment", "payment-service"}

_RISK_BY_LEVEL = {"low": 2.0, "medium": 5.0, "high": 8.0}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an attribute from an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass
class ActReport:
    severity: str
    severity_rationale: str
    plan_present: bool
    aggregate_decision: Optional[str]
    action_reports: List[Dict[str, Any]] = field(default_factory=list)
    executed: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_incident_signals(state: Any) -> IncidentSignals:
    """Best-effort mapping from graph state to severity signals.

    Conservative by design: it seeds impact/urgency from what is cheaply and
    reliably available (alert severity, affected service, reflector confidence)
    and lets the Severity Engine's defaults handle the rest. Richer signal
    extraction (live error-rate / burn-rate from the metrics results) is a
    Phase-1 enhancement.
    """
    alert = _get(state, "alert_context")
    labels = _get(alert, "labels", {}) or {}
    severity_label = str(_get(alert, "severity", labels.get("severity", "")) or "").lower()

    service = str(labels.get("service") or labels.get("app") or "").lower()
    namespace = labels.get("namespace")

    is_critical = severity_label == "critical"
    revenue = service in _REVENUE_SERVICES

    # Reflector confidence, when the ORIENT phase has run.
    reflector = _get(state, "reflector_analysis")
    confidence = _get(reflector, "confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0

    # Count distinct services mentioned across investigation results, floored at 1.
    agent_results = _get(state, "agent_results", {}) or {}
    affected_services = max(1, len({k for k in agent_results.keys()})) if agent_results else 1

    return IncidentSignals(
        affected_services=affected_services if service else 1,
        user_facing=revenue or is_critical,
        revenue_impacting=revenue,
        error_rate=0.6 if is_critical else 0.2,  # coarse seed from alert severity
        slo_breached=is_critical,
        slo_burn_rate=14.4 if is_critical else 1.0,
        saturation=0.0,
        still_escalating=is_critical,
        hypothesis_confidence=confidence,
    )


def _incident_environment(state: Any) -> str:
    alert = _get(state, "alert_context")
    labels = _get(alert, "labels", {}) or {}
    env = labels.get("environment") or labels.get("env") or labels.get("namespace") or "production"
    return str(env).lower()


def _plan_actions(plan: Any) -> List[Any]:
    actions = _get(plan, "actions", []) or []
    return list(actions)


def _plan_risk_score(plan: Any) -> float:
    level = str(_get(plan, "risk_level", "medium") or "medium").lower()
    return _RISK_BY_LEVEL.get(level, 5.0)


def build_act_report(
    state: Any,
    environment: Optional[str] = None,
    evaluate_fn: Optional[Callable[[Any, str, float], Tuple[bool, str]]] = None,
    dry_run: bool = True,
    actor: str = "sre-agent",
) -> ActReport:
    """Compute severity, gate the plan, and dry-run the autonomous actions."""
    assessment: SeverityAssessment = classify_severity(extract_incident_signals(state))
    incident_id = _get(state, "incident_id") or _get(_get(state, "metadata", {}) or {}, "incident_id")

    plan = _get(state, "remediation_plan")
    actions = _plan_actions(plan)

    if not plan or not actions:
        return ActReport(
            severity=assessment.severity.name,
            severity_rationale=assessment.rationale,
            plan_present=False,
            aggregate_decision=None,
            summary=f"{assessment.severity.name}: no remediation plan in state; ACT skipped.",
        )

    env = environment or _incident_environment(state)
    risk_score = _plan_risk_score(plan)

    aggregate, per_action = decide_plan(actions, assessment, env, risk_score, evaluate_fn)

    executor = Executor(actor=actor, incident_id=incident_id)
    action_reports: List[Dict[str, Any]] = []
    executed: List[Dict[str, Any]] = []

    for action, gd in zip(actions, per_action):
        rep: Dict[str, Any] = {
            "action_type": str(_get(action, "action_type", "")),
            "target": str(_get(action, "target", "")),
            "decision": gd.decision.value,
            "reversibility": gd.reversibility.value,
            "reason": gd.reason,
        }
        if gd.decision is AutonomyDecision.AUTONOMOUS:
            result = executor.execute(action, gd.decision.value, dry_run=dry_run)
            rep["command"] = result.command
            rep["audit_hash"] = result.audit.get("content_hash")
            rep["rollback_command"] = result.rollback_command
            executed.append(rep)
        action_reports.append(rep)

    summary = (
        f"{assessment.severity.name}: plan {aggregate.value}; "
        f"{len(executed)}/{len(actions)} action(s) dry-run-executed, "
        f"{len(actions) - len(executed)} held for approval/blocked."
    )
    logger.info(f"⚙️  ACT: {summary}")

    return ActReport(
        severity=assessment.severity.name,
        severity_rationale=assessment.rationale,
        plan_present=True,
        aggregate_decision=aggregate.value,
        action_reports=action_reports,
        executed=executed,
        summary=summary,
    )
