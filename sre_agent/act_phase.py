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

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .confidence_calibration import (
    ConfidenceCalibrationError,
    calibrate_confidence,
    load_calibration_artifact,
)
from .executor import Executor
from .execution_context import ExecutionContext
from .mutation_gateway import MutationGateContext, authorize_and_execute
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


def _raw_probability(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if 0 <= parsed <= 1 else None


def _configured_confidence(
    raw: Optional[float],
    *,
    task: str,
    environment_variable: str,
    calibration_path: Optional[Path] = None,
):
    configured = calibration_path
    if configured is None:
        value = os.getenv(environment_variable, "").strip()
        configured = Path(value) if value else None
    if raw is None or configured is None:
        return None
    config_fingerprint = os.getenv("SENTINEL_CONFIG_FINGERPRINT", "").strip()
    if not config_fingerprint:
        return None
    try:
        artifact = load_calibration_artifact(configured)
        return calibrate_confidence(
            raw,
            artifact,
            task=task,
            config_fingerprint=config_fingerprint,
        )
    except (ConfidenceCalibrationError, OSError) as exc:
        logger.error("%s confidence calibration unavailable: %s", task, exc)
        return None


@dataclass
class ActReport:
    severity: str
    severity_rationale: str
    plan_present: bool
    aggregate_decision: Optional[str]
    action_reports: List[Dict[str, Any]] = field(default_factory=list)
    executed: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    confidence_status: str = "uncalibrated"
    raw_action_confidence: Optional[float] = None
    calibrated_action_probability: Optional[float] = None
    minimum_autonomy_probability: Optional[float] = None
    calibration_artifact_version: Optional[str] = None
    calibration_artifact_sha256: Optional[str] = None

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

    # Raw reflector confidence is retained as evidence but cannot affect
    # severity until a diagnosis-specific calibration artifact maps it.
    reflector = _get(state, "reflector_analysis")
    raw_confidence = _raw_probability(_get(reflector, "confidence"))
    calibrated = _configured_confidence(
        raw_confidence,
        task="diagnosis",
        environment_variable="DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH",
    )

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
        hypothesis_confidence=(
            calibrated.calibrated_probability
            if calibrated is not None
            else 0.0
        ),
        hypothesis_confidence_calibrated=calibrated is not None,
    )


def _incident_environment(state: Any) -> str:
    # Alert labels are attacker/workload controlled and must never weaken policy.
    # Runtime construction writes this metadata from the operator-owned context;
    # missing or unknown values deliberately fail to production.
    metadata = _get(state, "metadata", {}) or {}
    raw = str(_get(metadata, "cluster_environment", "production") or "production").lower()
    aliases = {
        "prod": "production",
        "production": "production",
        "stage": "staging",
        "staging": "staging",
        "dev": "development",
        "development": "development",
        "test": "testing",
        "testing": "testing",
    }
    return aliases.get(raw, "production")


def _plan_actions(plan: Any) -> List[Any]:
    actions = _get(plan, "actions", []) or []
    return list(actions)


def _plan_risk_score(plan: Any) -> float:
    level = str(_get(plan, "risk_level", "medium") or "medium").lower()
    return _RISK_BY_LEVEL.get(level, 5.0)


def _configured_remediation_confidence(
    plan: Any,
    calibration_path: Optional[Path],
):
    raw = _raw_probability(_get(plan, "confidence"))
    calibrated = _configured_confidence(
        raw,
        task="remediation",
        environment_variable="REMEDIATION_CONFIDENCE_CALIBRATION_PATH",
        calibration_path=calibration_path,
    )
    return raw, calibrated


def build_act_report(
    state: Any,
    environment: Optional[str] = None,
    evaluate_fn: Optional[Callable[[Any, str, float], Tuple[bool, str]]] = None,
    dry_run: bool = True,
    actor: str = "sre-agent",
    calibration_path: Optional[Path] = None,
    calibrated_action_probability: Optional[float] = None,
    minimum_autonomy_probability: Optional[float] = None,
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
    raw_confidence, calibrated = _configured_remediation_confidence(
        plan, calibration_path
    )
    artifact_version = None
    artifact_sha256 = None
    if calibrated is not None:
        calibrated_action_probability = calibrated.calibrated_probability
        minimum_autonomy_probability = calibrated.autonomy_threshold
        artifact_version = calibrated.artifact_version
        artifact_sha256 = calibrated.artifact_sha256
    confidence_ready = (
        calibrated is not None
        and calibrated.autonomy_threshold is not None
    )

    aggregate, per_action = decide_plan(
        actions,
        assessment,
        env,
        risk_score,
        evaluate_fn,
        calibrated_action_probability,
        minimum_autonomy_probability,
    )

    # Per-cluster blast radius: when this cluster is namespace-scoped, remediation
    # may only touch its own namespace. Missing namespaces default to it; anything
    # targeting a different namespace is hard-blocked before any (even dry-run)
    # execution — the enforcement point that actually knows the cluster's scope.
    cluster_ns = str(_get(_get(state, "metadata", {}) or {}, "cluster_namespace") or "").strip()

    executor = Executor(actor=actor, incident_id=incident_id)
    action_reports: List[Dict[str, Any]] = []
    executed: List[Dict[str, Any]] = []
    blocked_out_of_scope = 0

    for action, gd in zip(actions, per_action):
        params = getattr(action, "parameters", None)
        action_ns = str((params or {}).get("namespace", "") or "").strip()

        if cluster_ns:
            if not action_ns:
                # Default an unscoped action to the cluster's namespace.
                if isinstance(params, dict):
                    params["namespace"] = cluster_ns
                action_ns = cluster_ns
            elif action_ns != cluster_ns:
                blocked_out_of_scope += 1
                action_reports.append({
                    "action_type": str(_get(action, "action_type", "")),
                    "target": str(_get(action, "target", "")),
                    "namespace": action_ns,
                    "parameters": dict(params or {}),
                    "decision": AutonomyDecision.BLOCKED.value,
                    "reversibility": gd.reversibility.value,
                    "reason": f"blocked: targets namespace '{action_ns}', outside this cluster's scope '{cluster_ns}'",
                })
                continue

        rep: Dict[str, Any] = {
            "action_type": str(_get(action, "action_type", "")),
            "target": str(_get(action, "target", "")),
            "namespace": action_ns or (cluster_ns or "default"),
            "parameters": dict(params or {}),
            "decision": gd.decision.value,
            "reversibility": gd.reversibility.value,
            "reason": gd.reason,
            "confidence_calibrated": gd.confidence_calibrated,
            "calibrated_action_probability": gd.calibrated_action_probability,
            "minimum_autonomy_probability": gd.minimum_autonomy_probability,
        }
        if gd.decision is AutonomyDecision.AUTONOMOUS:
            result = executor.execute(action, gd.decision.value, dry_run=dry_run)
            rep["command"] = result.command
            rep["audit_hash"] = result.audit.get("content_hash")
            rep["rollback_command"] = result.rollback_command
            executed.append(rep)
        action_reports.append(rep)

    scope_note = f", {blocked_out_of_scope} blocked out-of-namespace" if blocked_out_of_scope else ""
    summary = (
        f"{assessment.severity.name}: plan {aggregate.value}; "
        f"{len(executed)}/{len(actions)} action(s) dry-run-executed, "
        f"{len(actions) - len(executed)} held for approval/blocked{scope_note}."
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
        confidence_status="calibrated" if confidence_ready else "uncalibrated",
        raw_action_confidence=raw_confidence,
        calibrated_action_probability=calibrated_action_probability,
        minimum_autonomy_probability=minimum_autonomy_probability,
        calibration_artifact_version=artifact_version,
        calibration_artifact_sha256=artifact_sha256,
    )


async def execute_autonomous_live(
    state: Any,
    report: ActReport,
    tool_caller: Callable[[str, Dict[str, Any]], Any],
    actor: str = "sre-agent",
    github_caller: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    approved: bool = False,
    context: Optional[ExecutionContext] = None,
) -> List[Dict[str, Any]]:
    """Really apply the plan's autonomous actions via the MCP servers.

    Called when ``EXECUTOR_LIVE`` is enabled and the whole plan was autonomous,
    or after the graph's exact action hash was approved by an administrator.
    Hard-blocked actions are never applied, including after human approval.
    """
    plan = _get(state, "remediation_plan")
    actions = _plan_actions(plan)
    incident_id = _get(state, "incident_id") or _get(_get(state, "metadata", {}) or {}, "incident_id")
    metadata = _get(state, "metadata", {}) or {}
    approval = _get(metadata, "approval", {}) or {}
    approval_hash = str(_get(approval, "action_hash", "") or "")
    environment = str(getattr(context, "environment", "production") or "production")
    risk_score = _plan_risk_score(plan)

    results: List[Dict[str, Any]] = []
    for index, (action, arep) in enumerate(zip(actions, report.action_reports)):
        allowed_decisions = {AutonomyDecision.AUTONOMOUS.value}
        if approved:
            allowed_decisions.add(AutonomyDecision.REQUIRES_APPROVAL.value)
        if arep.get("decision") not in allowed_decisions:
            continue

        action_payload = {
            "organization_id": getattr(context, "organization_id", None),
            "cluster_id": getattr(context, "cluster_id", None),
            "incident_id": incident_id,
            "plan_id": _get(plan, "plan_id"),
            "action_index": index,
            "action_type": _get(action, "action_type", ""),
            "target": _get(action, "target", ""),
            "parameters": _get(action, "parameters", {}) or {},
            "approval_hash": approval_hash,
        }
        idempotency_key = hashlib.sha256(
            json.dumps(
                action_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        gate_context = MutationGateContext(
            decision=str(arep.get("decision", "")),
            severity=report.severity,
            environment=environment,
            risk_score=risk_score,
            approved=approved,
            actor=actor,
            incident_id=str(incident_id) if incident_id else None,
            raw_action_confidence=report.raw_action_confidence,
        )
        res = await authorize_and_execute(
            action,
            gate_context,
            context,
            tool_caller,
            github_caller,
            idempotency_key,
        )
        results.append({
            "action_type": res.action_type,
            "target": res.target,
            "status": res.status,
            "command": res.command,
            "detail": res.detail,
        })
    return results


def apply_skill_learning(state: Any, report: ActReport, store: Any = None) -> Dict[str, Any]:
    """Self-improving loop (project #2): propose prior skills, record this one.

    Proposes skills learned from *earlier* incidents of the same class, then
    records the actions applied in *this* incident as a (possibly recurring)
    skill. ``store`` is injectable for testing; defaults to the process store.
    """
    from .skill_store import get_skill_store, propose_skills, record_successful_remediation

    store = store or get_skill_store()
    alert = _get(state, "alert_context")
    incident_id = _get(state, "incident_id") or _get(_get(state, "metadata", {}) or {}, "incident_id")

    proposed = propose_skills(store, alert)  # from prior incidents, before recording this one
    executed = getattr(report, "executed", None) or []
    recorded = record_successful_remediation(store, alert, executed, incident_id) if executed else None

    return {
        "proposed_skills": [s.brief() for s in proposed],
        "recorded_skill": recorded.brief() if recorded else None,
    }


async def verify_live(state: Any, tool_caller: Any, wait_seconds: int = 0) -> Dict[str, Any]:
    """Confirm a remediation worked by re-checking the incident's error rate.

    Builds an error-rate PromQL for the affected service, re-queries it through
    the injected Prometheus tool_caller, and evaluates RESOLVED/FAILED against a
    configurable threshold. Injected caller → testable without a live cluster.
    """
    from .nl_query import QueryIntent, build_promql
    from .skill_store import signature_from_alert
    from .verification import verify_remediation

    alert = _get(state, "alert_context")
    service = signature_from_alert(alert).service
    promql = build_promql(QueryIntent("error_rate", None if service == "unknown" else service, "5m"))
    threshold = float(os.getenv("VERIFY_ERROR_THRESHOLD", "0.05"))

    outcome = await verify_remediation(promql, threshold, tool_caller, wait_seconds=wait_seconds)
    return {
        "status": outcome.status,
        "current_value": outcome.current_value,
        "threshold": threshold,
        "improvement_pct": outcome.improvement_pct,
        "detail": outcome.detail,
        "promql": promql,
    }
