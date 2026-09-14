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

import copy
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field, is_dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .confidence_calibration import (
    ConfidenceCalibrationError,
    calibrate_confidence,
    load_calibration_artifact,
)
from .execution_context import ExecutionContext
from .executor import (
    EXECUTOR_TOOL_MAP,
    NON_MUTATING_ACTIONS,
    NOTIFY_ONLY_ACTIONS,
    Executor,
    live_tool_for_action,
    missing_capability_reason,
)
from .mutation_gateway import MutationGateContext, MutationRejected, authorize_and_execute
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


def _with_target(action: Any, target: str) -> Any:
    """Return a copy of ``action`` with ``.target`` overridden.

    Tolerates pydantic models (real ``RemediationAction``), dataclasses and
    plain dicts (test doubles), and generic objects.
    """
    if isinstance(action, dict):
        return {**action, "target": target}
    if is_dataclass(action) and not isinstance(action, type):
        return dataclass_replace(action, target=target)
    if hasattr(action, "model_copy"):
        return action.model_copy(update={"target": target})
    if hasattr(action, "copy"):
        try:
            return action.copy(update={"target": target})
        except TypeError:
            pass
    clone = copy.copy(action)
    try:
        clone.target = target
    except Exception:
        return action
    return clone


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
    unknown_telemetry: bool = False
    severity_evidence: List[Dict[str, Any]] = field(default_factory=list)
    confidence_status: str = "uncalibrated"
    raw_action_confidence: Optional[float] = None
    calibrated_action_probability: Optional[float] = None
    minimum_autonomy_probability: Optional[float] = None
    calibration_artifact_version: Optional[str] = None
    calibration_artifact_sha256: Optional[str] = None
    # The planner's own error, when the "plan" being gated is the fallback
    # placeholder rather than anything the planner produced. Carried through
    # so the approval message can say so — see RemediationPlan.planning_failed.
    planning_failed: Optional[str] = None

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
    severity_label = str(
        _get(alert, "severity", labels.get("severity", "")) or ""
    ).lower()

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

    # Raw reflector confidence is retained as evidence but cannot affect
    # severity until a diagnosis-specific calibration artifact maps it.
    reflector = _get(state, "reflector_analysis")
    raw_confidence = _raw_probability(_get(reflector, "confidence"))
    calibrated = _configured_confidence(
        raw_confidence,
        task="diagnosis",
        environment_variable="DIAGNOSIS_CONFIDENCE_CALIBRATION_PATH",
    )

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
        hypothesis_confidence_calibrated=calibrated is not None,
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
        calibrated is not None and calibrated.autonomy_threshold is not None
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
    cluster_ns = str(
        _get(_get(state, "metadata", {}) or {}, "cluster_namespace") or ""
    ).strip()

    executor = Executor(actor=actor, incident_id=incident_id)
    action_reports: List[Dict[str, Any]] = []
    executed: List[Dict[str, Any]] = []
    blocked_out_of_scope = 0
    blocked_no_capability = 0

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
            "confidence_calibrated": gd.confidence_calibrated,
            "calibrated_action_probability": gd.calibrated_action_probability,
            "minimum_autonomy_probability": gd.minimum_autonomy_probability,
        }
        # A live-mutating action Sentinel has no tool for belongs in the plan a
        # human reads, not in the execution log after they approved it. The
        # `config_change`/`patch` pair is what bites: the only tool behind them
        # changes container cpu/memory limits, so a change to an env var,
        # ConfigMap or feature flag — what "config change" usually means, and
        # what the planner emits — has nothing behind it. Dry-running it would
        # print a `kubectl apply -f <rendered-config>` placeholder as if it were
        # a command, which is the same lie one step earlier.
        if rep["action_type"].lower() in EXECUTOR_TOOL_MAP and live_tool_for_action(action) is None:
            blocked_no_capability += 1
            rep["decision"] = AutonomyDecision.BLOCKED.value
            rep["reason"] = missing_capability_reason(rep["action_type"].lower())
            action_reports.append(rep)
            continue

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
    # Named separately from the out-of-namespace block: one is this cluster
    # refusing a target, the other is Sentinel having no tool at all. Collapsing
    # them into "blocked" hides a capability gap as if it were a policy call.
    capability_note = (
        f", {blocked_no_capability} blocked (no automation capability)"
        if blocked_no_capability
        else ""
    )
    summary = (
        f"{assessment.severity.name}: plan {aggregate.value}; "
        f"{len(executed)}/{len(actions)} action(s) dry-run-executed, "
        f"{len(actions) - len(executed)} held for approval/blocked"
        f"{scope_note}{capability_note}."
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
        confidence_status="calibrated" if confidence_ready else "uncalibrated",
        raw_action_confidence=raw_confidence,
        calibrated_action_probability=calibrated_action_probability,
        minimum_autonomy_probability=minimum_autonomy_probability,
        calibration_artifact_version=artifact_version,
        calibration_artifact_sha256=artifact_sha256,
        planning_failed=(_get(plan, "planning_failed") or None),
    )


async def _execute_notify_only(
    action: Any,
    action_payload: Dict[str, Any],
    incident_id: Optional[str],
) -> Dict[str, Any]:
    """Deliver a notify-only action by paging the humans in the incident thread.

    Slack is this platform's only communication channel, so an escalation goes
    out the way every other agent message does: as a surfaced ``act`` timeline
    event, which ``war_room.forward_events`` streams into the incident's Slack
    thread (and ``live_events`` mirrors to the dashboard).

    Honest about its failure case — with no incident thread there is no on-call
    to reach, and the result says so instead of reporting a page that never
    went out.
    """
    from .executor import build_command

    action_type = str(action_payload.get("action_type") or "").lower()
    target = str(action_payload.get("target") or "")
    command = build_command(action)

    def _result(status: str, detail: str) -> Dict[str, Any]:
        return {
            "action_type": action_type,
            "target": target,
            "status": status,
            "command": command,
            "detail": detail,
        }

    if not incident_id:
        return _result("SKIPPED", "No incident thread to escalate into.")

    params = action_payload.get("parameters") or {}
    why = str(
        params.get("reason")
        or params.get("details")
        or _get(action, "safety_check", "")
        or ""
    ).strip()
    body = "\n".join(
        line
        for line in (
            f"This remediation plan needs a human on *{target}*.",
            why,
            "Reply in this thread to take it over.",
        )
        if line
    )
    try:
        from .incident_timeline import emit_timeline_event

        event = await emit_timeline_event(
            incident_id,
            event_type="act",
            speaker_role="executor",
            title="🚨 Escalation to on-call",
            content=body,
            payload={
                "source": "act_phase",
                "action_type": action_type,
                "target": target,
            },
        )
    except Exception as exc:  # pragma: no cover - emission is itself guarded
        logger.warning("Escalation notice for %s failed: %s", incident_id, exc)
        return _result("ERROR", f"Escalation notice failed: {exc}")
    if event is None:
        return _result(
            "ERROR", "Escalation notice could not be posted to the incident thread."
        )
    logger.info("📣 ACT: escalated '%s' to on-call in the incident thread", target)
    return _result("EXECUTED", f"Paged on-call in the incident thread about '{target}'.")


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

    # The Planner's `target` field is free text (a display description, not a
    # schema-validated k8s object name) and has been observed to contain
    # colon-suffixed sub-resource descriptors, pod names with random hash
    # suffixes, and trailing parenthetical clarifications — none of which are
    # valid k8s resource names. The alert's own `service`/`app` label is the
    # one canonical, non-LLM-generated resource identifier tied to this
    # incident, so k8s-mutating actions use it as the live target instead of
    # trusting the Planner's free text.
    alert_labels = _get(_get(state, "alert_context"), "labels", {}) or {}
    canonical_service = str(
        alert_labels.get("service") or alert_labels.get("app") or ""
    ).strip()

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
        # Notify-only actions never reach the mutation gateway: they mutate
        # nothing, so there is no namespace to scope-check and no idempotency
        # key to dedupe, and the gateway would reject them outright.
        if str(action_payload["action_type"]).lower() in NOTIFY_ONLY_ACTIONS:
            results.append(
                await _execute_notify_only(action, action_payload, incident_id)
            )
            continue
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
        live_action = action
        if canonical_service and action_payload["action_type"] in EXECUTOR_TOOL_MAP:
            live_action = _with_target(action, canonical_service)
        try:
            res = await authorize_and_execute(
                live_action,
                gate_context,
                context,
                tool_caller,
                github_caller,
                idempotency_key,
            )
        except MutationRejected as exc:
            # A rejection at the authorization boundary (e.g. an action type
            # with no live tool mapping) is per-action, not systemic — record
            # it and keep executing the rest of the plan's actions instead of
            # aborting the whole batch.
            logger.warning(
                "Live action %d (%s on %s) rejected: %s: %s",
                index,
                action_payload["action_type"],
                action_payload["target"],
                exc.code,
                exc.detail,
            )
            results.append(
                {
                    "action_type": action_payload["action_type"],
                    "target": action_payload["target"],
                    "status": "REFUSED",
                    "command": "",
                    "detail": f"{exc.code}: {exc.detail}",
                }
            )
            continue
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
    state: Any,
    report: ActReport,
    store: Any = None,
    *,
    verification_outcome: Any = None,
    live_results: Any = None,
    incident_status: Any = None,
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    human_approved: Optional[bool] = None,
) -> Dict[str, Any]:
    """Self-improving loop: propose prior skills; record only verified successes.

    Dry-run, blocked, failed, and unknown outcomes become negative exemplars and
    never increment successful skill counts. ``store`` is injectable for tests.
    """
    from .skill_store import (
        get_skill_store,
        propose_skills,
        record_successful_remediation,
    )
    from .verified_learning import (
        assess_learning_eligibility,
        build_negative_exemplar,
        live_executed_actions,
    )

    store = store or get_skill_store()
    alert = _get(state, "alert_context")
    incident_id = _get(state, "incident_id") or _get(
        _get(state, "metadata", {}) or {}, "incident_id"
    )
    metadata = _get(state, "metadata", {}) or {}
    verification = verification_outcome
    if verification is None:
        verification = _get(report, "verification") or metadata.get("verification")
    if live_results is None:
        live_results = _get(report, "live_results") or metadata.get("live_results")
    if config_fingerprint is None:
        config_fingerprint = (
            os.getenv("SENTINEL_CONFIG_FINGERPRINT", "").strip() or None
        )
    if run_manifest_sha256 is None:
        run_manifest_sha256 = metadata.get("run_manifest_sha256")
    organization_id = metadata.get("organization_id")
    cluster_id = metadata.get("cluster_id")

    proposed = propose_skills(
        store, alert, organization_id=organization_id, cluster_id=cluster_id
    )
    if human_approved is None:
        # Only the graph verifies the approval's action_hash against the plan
        # it ran; without that boolean, fall back to the durable record.
        human_approved = _get(metadata.get("approval") or {}, "status") == "approved"
    eligibility = assess_learning_eligibility(
        act_report=report,
        verification_outcome=verification,
        incident_status=incident_status,
        live_results=live_results,
        executed=getattr(report, "executed", None) or [],
        human_approved=human_approved,
    )
    recorded = None
    negative = None
    live = live_executed_actions(
        live_results=live_results,
        executed=getattr(report, "executed", None) or [],
    )
    if eligibility.eligible_for_success and live:
        recorded = record_successful_remediation(
            store,
            alert,
            live,
            incident_id,
            organization_id=organization_id,
            cluster_id=cluster_id,
            verification_status=eligibility.verification_status or "RESOLVED",
            reviewer_id=reviewer_id,
            run_manifest_sha256=run_manifest_sha256,
            config_fingerprint=config_fingerprint,
        )
    elif incident_id:
        try:
            negative = build_negative_exemplar(
                eligibility=eligibility,
                incident_id=str(incident_id),
                summary=getattr(report, "summary", "") or eligibility.outcome_class,
                actions=live
                or list(getattr(report, "executed", None) or [])
                or list(getattr(report, "action_reports", None) or []),
                reviewer_id=reviewer_id,
                run_manifest_sha256=run_manifest_sha256,
                config_fingerprint=config_fingerprint,
            )
            store.add_negative(negative.to_dict())
        except Exception as exc:
            logger.warning("negative exemplar not recorded: %s", exc)
            negative = None

    return {
        "proposed_skills": [s.brief() for s in proposed],
        "recorded_skill": recorded.brief() if recorded else None,
        "negative_exemplar": negative.to_dict() if negative else None,
        "learning_eligibility": eligibility.to_dict(),
    }


def live_outcome_summary(report_payload: Dict[str, Any]) -> str:
    """One line saying what actually ran, for the message a human reads after
    approving.

    ``ActReport.summary`` is written at plan time — "1/4 dry-run-executed, 3
    held for approval/blocked" — and stops being true the moment someone
    approves. Posting it unchanged into the Slack thread after a live run told
    the on-call that nothing had run while four actions were executing against
    their cluster. Mutations are counted apart from pages and reads, for the
    same reason verification excludes them: a page is not a fix.
    """
    live_results = [
        item for item in (report_payload.get("live_results") or []) if isinstance(item, dict)
    ]
    if not live_results:
        return str(report_payload.get("summary") or "")

    severity = str(report_payload.get("severity") or "UNKNOWN")
    mutating = [
        item
        for item in live_results
        if str(item.get("action_type", "")).lower() not in NON_MUTATING_ACTIONS
    ]
    other = len(live_results) - len(mutating)
    executed = sum(1 for item in mutating if str(item.get("status")) == "EXECUTED")

    parts = [
        f"{severity}: approved plan executed live — "
        f"{executed}/{len(mutating)} mutating action(s) EXECUTED"
    ]
    failures = sorted(
        {
            str(item.get("status"))
            for item in live_results
            if str(item.get("status")) not in {"EXECUTED", "None"}
        }
    )
    if failures:
        parts.append(f" [{', '.join(failures)}]")
    if other:
        parts.append(f", {other} notification/read-only action(s) delivered")
    verification = report_payload.get("verification") or {}
    if verification:
        parts.append(
            f". Verification: {verification.get('status', 'UNKNOWN')}"
            f" ({verification.get('detail', 'no detail')})"
        )
    else:
        parts.append(". Verification: not run (nothing mutating executed)")
    if report_payload.get("live_error"):
        parts.append(f". Live error: {report_payload['live_error']}")
    return "".join(parts)


async def verify_live(
    state: Any, tool_caller: Any, wait_seconds: int = 0
) -> Dict[str, Any]:
    """Confirm a remediation worked by waiting for the incident's alert to clear.

    The alert that opened the incident is the objective a human would watch to
    call it over, and the only threshold here that isn't invented — so that is
    what gets re-checked, polled until it clears or the budget runs out.
    Incidents with no alert behind them (a manually opened one) fall back to the
    service's error rate. Injected caller → testable without a live cluster.
    """
    from .nl_query import QueryIntent, build_promql
    from .skill_store import signature_from_alert
    from .verification import alert_state_promql, verify_alert_cleared, verify_remediation

    alert = _get(state, "alert_context")
    service = signature_from_alert(alert).service
    alert_name = str(_get(alert, "alert_name", "") or "").strip()

    if alert_name:
        outcome = await verify_alert_cleared(
            alert_name,
            service,
            tool_caller,
            settle_seconds=wait_seconds,
            # The budget has to outlast the alert's own averaging window, not
            # just the rollout. Typical rules here read
            # `rate(...[5m]) ... for: 3m`, so a *correct* fix cannot clear the
            # alert for ~5 minutes afterwards no matter how fast the new pod
            # comes up. At 300s a live slow-query remediation that worked was
            # graded FAILED and the incident moved to REMEDIATION_FAILED — the
            # alert cleared 40s later. Grading a working fix as broken is the
            # expensive direction of this error, so the default now covers the
            # window plus a rollout.
            timeout_seconds=int(os.getenv("VERIFY_ALERT_TIMEOUT_SECONDS", "600")),
            poll_seconds=int(os.getenv("VERIFY_ALERT_POLL_SECONDS", "30")),
        )
        return {
            "status": outcome.status,
            "current_value": outcome.current_value,
            "threshold": outcome.threshold,
            "improvement_pct": outcome.improvement_pct,
            "detail": outcome.detail,
            "promql": alert_state_promql(alert_name, service),
            "signal": "alert_state",
            "alert_name": alert_name,
        }

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
        "signal": "error_rate",
    }
