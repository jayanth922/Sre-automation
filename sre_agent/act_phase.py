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
from typing import Any, Callable, Dict, List, Optional, Tuple

from .execution_context import ExecutionContext
from .executor import Executor
from .mutation_gateway import MutationGateContext, authorize_and_execute
from .policy_gate import AutonomyDecision, decide_plan
from .severity_engine import (
    EvidenceLink,
    IncidentSignals,
    SeverityAssessment,
    classify_severity,
    evidence,
)

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


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _walk_metrics(obj: Any) -> Dict[str, Any]:
    """Collect known metric keys from nested investigation payloads."""
    found: Dict[str, Any] = {}
    keys = {
        "error_rate",
        "slo_burn_rate",
        "burn_rate",
        "saturation",
        "error_rate_slope",
        "affected_pods",
        "affected_services",
        "slo_breached",
        "still_escalating",
        "duration_seconds",
        "dependency_count",
        "customer_scope",
    }

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_str = str(key)
                child_path = f"{path}.{key_str}" if path else key_str
                if key_str in keys and key_str not in found:
                    found[key_str] = (value, child_path)
                visit(value, child_path)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                visit(item, f"{path}[{idx}]")

    visit(obj, "")
    return found


@dataclass
class ActReport:
    severity: str
    severity_rationale: str
    plan_present: bool
    aggregate_decision: Optional[str]
    action_reports: List[Dict[str, Any]] = field(default_factory=list)
    executed: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    unknown_telemetry: bool = False
    severity_evidence: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_incident_signals(state: Any) -> IncidentSignals:
    """Map graph state to severity signals using measured evidence only.

    Alert severity labels may inform qualitative business scope (e.g. known
    revenue services) but must never invent error_rate / burn_rate / breadth.
    Missing telemetry remains ``None`` so the severity engine escalates to
    UNKNOWN instead of fabricating calm or critical numbers.
    """
    links: List[EvidenceLink] = []
    alert = _get(state, "alert_context")
    labels = _get(alert, "labels", {}) or {}
    annotations = _get(alert, "annotations", {}) or {}

    service = str(labels.get("service") or labels.get("app") or "").lower()
    revenue = service in _REVENUE_SERVICES if service else None
    if revenue is not None:
        links.append(
            evidence(
                "revenue_impacting",
                revenue,
                source=f"service_catalog:{service or 'unknown'}",
            )
        )
        links.append(
            evidence(
                "user_facing",
                revenue,
                source=f"service_catalog:{service or 'unknown'}",
            )
        )

    # Explicit measured values from alert annotations / labels only when present.
    measured = {
        "error_rate": _as_float(
            labels.get("error_rate") or annotations.get("error_rate")
        ),
        "slo_burn_rate": _as_float(
            labels.get("slo_burn_rate")
            or labels.get("burn_rate")
            or annotations.get("slo_burn_rate")
            or annotations.get("burn_rate")
        ),
        "saturation": _as_float(
            labels.get("saturation") or annotations.get("saturation")
        ),
        "error_rate_slope": _as_float(
            labels.get("error_rate_slope") or annotations.get("error_rate_slope")
        ),
        "affected_pods": _as_int(
            labels.get("affected_pods") or annotations.get("affected_pods")
        ),
        "affected_services": _as_int(
            labels.get("affected_services") or annotations.get("affected_services")
        ),
        "slo_breached": _as_bool(
            labels.get("slo_breached") or annotations.get("slo_breached")
        ),
        "still_escalating": _as_bool(
            labels.get("still_escalating") or annotations.get("still_escalating")
        ),
        "duration_seconds": _as_float(
            labels.get("duration_seconds") or annotations.get("duration_seconds")
        ),
        "dependency_count": _as_int(
            labels.get("dependency_count") or annotations.get("dependency_count")
        ),
        "customer_scope": (
            str(labels.get("customer_scope") or annotations.get("customer_scope") or "")
            or None
        ),
    }

    # Prefer structured metrics from investigation results over labels.
    agent_results = _get(state, "agent_results", {}) or {}
    discovered = _walk_metrics(agent_results)
    for key, (value, path) in discovered.items():
        if key == "burn_rate":
            key = "slo_burn_rate"
        if key == "slo_breached" or key == "still_escalating":
            parsed = _as_bool(value)
        elif key in {"affected_pods", "affected_services", "dependency_count"}:
            parsed = _as_int(value)
        elif key == "customer_scope":
            parsed = str(value) if value is not None else None
        else:
            parsed = _as_float(value)
        if parsed is None:
            continue
        measured[key] = parsed
        links.append(evidence(key, parsed, source=f"agent_results:{path}"))

    for key, value in measured.items():
        if value is None:
            links.append(evidence(key, None, source="missing", unknown=True))
        elif not any(link.field == key and not link.unknown for link in links):
            links.append(evidence(key, value, source="alert_labels_or_annotations"))

    # Named service from labels is one affected service — not agent_result keys.
    if measured["affected_services"] is None and service:
        measured["affected_services"] = 1
        links.append(
            evidence(
                "affected_services",
                1,
                source=f"alert_label:service={service}",
            )
        )

    reflector = _get(state, "reflector_analysis")
    confidence = _as_float(_get(reflector, "confidence"))
    if confidence is not None:
        links.append(
            evidence("hypothesis_confidence", confidence, source="reflector_analysis")
        )
    else:
        links.append(
            evidence(
                "hypothesis_confidence",
                None,
                source="missing",
                unknown=True,
            )
        )

    return IncidentSignals(
        affected_services=measured["affected_services"],
        affected_pods=measured["affected_pods"],
        user_facing=revenue,
        revenue_impacting=revenue,
        error_rate=measured["error_rate"],
        slo_breached=measured["slo_breached"],
        customer_scope=measured["customer_scope"],
        dependency_count=measured["dependency_count"],
        duration_seconds=measured["duration_seconds"],
        slo_burn_rate=measured["slo_burn_rate"],
        error_rate_slope=measured["error_rate_slope"],
        saturation=measured["saturation"],
        still_escalating=measured["still_escalating"],
        hypothesis_confidence=confidence,
        evidence=links,
    )


def _incident_environment(state: Any) -> str:
    # Alert labels are attacker/workload controlled and must never weaken policy.
    # Runtime construction writes this metadata from the operator-owned context;
    # missing or unknown values deliberately fail to production.
    metadata = _get(state, "metadata", {}) or {}
    raw = str(
        _get(metadata, "cluster_environment", "production") or "production"
    ).lower()
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


def build_act_report(
    state: Any,
    environment: Optional[str] = None,
    evaluate_fn: Optional[Callable[[Any, str, float], Tuple[bool, str]]] = None,
    dry_run: bool = True,
    actor: str = "sre-agent",
) -> ActReport:
    """Compute severity, gate the plan, and dry-run the autonomous actions."""
    assessment: SeverityAssessment = classify_severity(extract_incident_signals(state))
    incident_id = _get(state, "incident_id") or _get(
        _get(state, "metadata", {}) or {}, "incident_id"
    )

    plan = _get(state, "remediation_plan")
    actions = _plan_actions(plan)

    if not plan or not actions:
        return ActReport(
            severity=assessment.severity.name,
            severity_rationale=assessment.rationale,
            plan_present=False,
            aggregate_decision=None,
            summary=f"{assessment.severity.name}: no remediation plan in state; ACT skipped.",
            unknown_telemetry=assessment.unknown_telemetry,
            severity_evidence=[item.to_dict() for item in assessment.evidence],
        )

    env = environment or _incident_environment(state)
    risk_score = _plan_risk_score(plan)

    aggregate, per_action = decide_plan(
        actions, assessment, env, risk_score, evaluate_fn
    )

    # Per-cluster blast radius: when this cluster is namespace-scoped, remediation
    # may only touch its own namespace. Missing namespaces default to it; anything
    # targeting a different namespace is hard-blocked before any (even dry-run)
    # execution — the enforcement point that actually knows the cluster's scope.
    cluster_ns = str(
        _get(_get(state, "metadata", {}) or {}, "cluster_namespace") or ""
    ).strip()

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
                action_reports.append(
                    {
                        "action_type": str(_get(action, "action_type", "")),
                        "target": str(_get(action, "target", "")),
                        "namespace": action_ns,
                        "parameters": dict(params or {}),
                        "decision": AutonomyDecision.BLOCKED.value,
                        "reversibility": gd.reversibility.value,
                        "reason": f"blocked: targets namespace '{action_ns}', outside this cluster's scope '{cluster_ns}'",
                    }
                )
                continue

        rep: Dict[str, Any] = {
            "action_type": str(_get(action, "action_type", "")),
            "target": str(_get(action, "target", "")),
            "namespace": action_ns or (cluster_ns or "default"),
            "parameters": dict(params or {}),
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

    scope_note = (
        f", {blocked_out_of_scope} blocked out-of-namespace"
        if blocked_out_of_scope
        else ""
    )
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
        unknown_telemetry=assessment.unknown_telemetry,
        severity_evidence=[item.to_dict() for item in assessment.evidence],
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
    incident_id = _get(state, "incident_id") or _get(
        _get(state, "metadata", {}) or {}, "incident_id"
    )
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
        )
        res = await authorize_and_execute(
            action,
            gate_context,
            context,
            tool_caller,
            github_caller,
            idempotency_key,
        )
        results.append(
            {
                "action_type": res.action_type,
                "target": res.target,
                "status": res.status,
                "command": res.command,
                "detail": res.detail,
            }
        )
    return results


def apply_skill_learning(
    state: Any, report: ActReport, store: Any = None
) -> Dict[str, Any]:
    """Self-improving loop (project #2): propose prior skills, record this one.

    Proposes skills learned from *earlier* incidents of the same class, then
    records the actions applied in *this* incident as a (possibly recurring)
    skill. ``store`` is injectable for testing; defaults to the process store.
    """
    from .skill_store import (
        get_skill_store,
        propose_skills,
        record_successful_remediation,
    )

    store = store or get_skill_store()
    alert = _get(state, "alert_context")
    incident_id = _get(state, "incident_id") or _get(
        _get(state, "metadata", {}) or {}, "incident_id"
    )

    proposed = propose_skills(
        store, alert
    )  # from prior incidents, before recording this one
    executed = getattr(report, "executed", None) or []
    recorded = (
        record_successful_remediation(store, alert, executed, incident_id)
        if executed
        else None
    )

    return {
        "proposed_skills": [s.brief() for s in proposed],
        "recorded_skill": recorded.brief() if recorded else None,
    }


async def verify_live(
    state: Any, tool_caller: Any, wait_seconds: int = 0
) -> Dict[str, Any]:
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
    promql = build_promql(
        QueryIntent("error_rate", None if service == "unknown" else service, "5m")
    )
    threshold = float(os.getenv("VERIFY_ERROR_THRESHOLD", "0.05"))

    outcome = await verify_remediation(
        promql, threshold, tool_caller, wait_seconds=wait_seconds
    )
    return {
        "status": outcome.status,
        "current_value": outcome.current_value,
        "threshold": threshold,
        "improvement_pct": outcome.improvement_pct,
        "detail": outcome.detail,
        "promql": promql,
    }
