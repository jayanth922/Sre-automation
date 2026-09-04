#!/usr/bin/env python3
"""
Policy Gate — decides HOW a remediation action may be executed.

This is the ACT-phase gate that sits between the Planner (which proposes a
``RemediationPlan``) and the Executor (which carries actions out). It answers a
single question per action:

    AUTONOMOUS  — safe for the agent to execute without a human
    REQUIRES_APPROVAL — must wait for a human at the checkpoint
    BLOCKED     — a hard policy rule forbids it entirely

The decision combines four independent checks, most-restrictive-wins:

1. **Hard policy** (delegated to ``policy_engine.evaluate_action``) — existing
   deterministic allow/deny rules, e.g. never scale-to-0 in prod. A block here
   is final.
2. **Calibrated-confidence gate** — self-reported confidence never authorizes a
   mutation. A task-specific calibrated remediation probability and measured
   threshold are required; otherwise the action waits for approval.
3. **Severity gate** (``severity_engine``) — autonomy is only offered for
   low-severity incidents. This is Jayanth's core requirement: low severity →
   autonomous, higher severity → approval.
4. **Reversibility floor** — even at low severity, an *irreversible* action is
   never auto-executed, and a *risky* action is auto-executed only if it carries
   a concrete rollback plan. This is what makes the autonomy defensible.

The module is dependency-light: it operates on any object exposing
``action_type`` (str), ``target`` (str), ``parameters`` (dict) and an optional
``rollback_plan``. The real ``RemediationAction`` (pydantic) satisfies this, and
tests can pass simple stand-ins — so this file needs no LLM/infra imports.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple

from .severity_engine import Severity, SeverityAssessment, is_low_severity

logger = logging.getLogger(__name__)


class AutonomyDecision(str, Enum):
    AUTONOMOUS = "autonomous"
    REQUIRES_APPROVAL = "requires_approval"
    BLOCKED = "blocked"


class Reversibility(str, Enum):
    REVERSIBLE = "reversible"  # trivially undone (restart, rollback, revert)
    RISKY = "risky"  # undoable only with a plan (config/patch/scale)
    IRREVERSIBLE = "irreversible"  # cannot be safely undone (scale-to-0, destructive)


# Baseline reversibility per action_type (from RemediationAction's action_type
# enum: restart, scale, rollback, config_change, patch, escalate, revert_commit,
# recreate_pod).
_BASE_REVERSIBILITY: dict[str, Reversibility] = {
    "restart": Reversibility.REVERSIBLE,
    "rollback": Reversibility.REVERSIBLE,
    "revert_commit": Reversibility.REVERSIBLE,
    "escalate": Reversibility.REVERSIBLE,  # notify-only; no infra mutation
    # Controller-owned pod, recreated immediately — same reversibility class as
    # restart, and narrower blast radius (one pod, not the whole deployment).
    "recreate_pod": Reversibility.REVERSIBLE,
    "scale": Reversibility.RISKY,  # reversible unless scaling to 0
    "config_change": Reversibility.RISKY,
    "patch": Reversibility.RISKY,
}


@dataclass
class GateDecision:
    decision: AutonomyDecision
    severity: Severity
    reversibility: Reversibility
    allowed_by_policy: bool
    reason: str
    confidence_calibrated: bool = False
    calibrated_action_probability: Optional[float] = None
    minimum_autonomy_probability: Optional[float] = None


def _has_rollback_plan(action: Any) -> bool:
    plan = getattr(action, "rollback_plan", None)
    return bool(plan and str(plan).strip())


def _replicas(action: Any) -> Optional[int]:
    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, dict):
        return None
    val = params.get("replicas", params.get("replica_count"))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def classify_reversibility(action: Any) -> Reversibility:
    """Classify how safely an action can be undone.

    Applies parameter-aware overrides on top of the per-type baseline — the most
    important being scale-to-0, which is treated as irreversible (service outage)
    regardless of the fact that "scale" is normally only risky.
    """
    action_type = str(getattr(action, "action_type", "")).lower()
    base = _BASE_REVERSIBILITY.get(action_type, Reversibility.RISKY)

    # scale-to-0 is an outage: escalate to irreversible.
    if action_type == "scale" and _replicas(action) == 0:
        return Reversibility.IRREVERSIBLE

    # A risky action with a concrete rollback plan is de-risked one notch.
    if base is Reversibility.RISKY and _has_rollback_plan(action):
        return Reversibility.RISKY  # stays risky, but the gate will allow it if low sev

    return base


def _default_policy_eval(
    action: Any, environment: str, risk_score: float
) -> Tuple[bool, str]:
    """Lazily delegate to the existing deterministic policy engine.

    Imported lazily so this module stays free of the ``agent_state`` /
    langchain import chain for unit tests. Callers may inject their own
    ``evaluate_fn`` to bypass this entirely.
    """
    from .policy_engine import evaluate_action  # lazy

    return evaluate_action(action, environment, risk_score)


def decide(
    action: Any,
    severity_assessment: SeverityAssessment,
    environment: str = "production",
    risk_score: float = 0.0,
    evaluate_fn: Optional[Callable[[Any, str, float], Tuple[bool, str]]] = None,
    calibrated_action_probability: Optional[float] = None,
    minimum_autonomy_probability: Optional[float] = None,
) -> GateDecision:
    """Decide how a single action may be executed. Most-restrictive-wins."""
    severity = severity_assessment.severity
    eval_fn = evaluate_fn or _default_policy_eval

    # 1. Hard policy — a block here is final.
    allowed, policy_reason = eval_fn(action, environment, risk_score)
    if not allowed:
        return GateDecision(
            decision=AutonomyDecision.BLOCKED,
            severity=severity,
            reversibility=classify_reversibility(action),
            allowed_by_policy=False,
            reason=f"Blocked by policy: {policy_reason}",
        )

    reversibility = classify_reversibility(action)

    # Unknown telemetry never grants autonomy — escalate to human approval.
    if severity is Severity.UNKNOWN or getattr(
        severity_assessment, "unknown_telemetry", False
    ):
        return GateDecision(
            decision=AutonomyDecision.REQUIRES_APPROVAL,
            severity=severity,
            reversibility=reversibility,
            allowed_by_policy=True,
            reason=(
                f"{severity.name}: unknown or incomplete telemetry; "
                "human approval required (no fabricated severity autonomy)"
            ),
        )

    low_sev = is_low_severity(severity)
    action_type = str(getattr(action, "action_type", "")).lower()

    # 2. A model's self-reported confidence is not authorization. Mutation
    # autonomy requires a task-specific calibration artifact with a measured
    # threshold. Notify-only escalation remains non-mutating.
    confidence_valid = (
        isinstance(calibrated_action_probability, (int, float))
        and not isinstance(calibrated_action_probability, bool)
        and isinstance(minimum_autonomy_probability, (int, float))
        and not isinstance(minimum_autonomy_probability, bool)
        and math.isfinite(float(calibrated_action_probability))
        and math.isfinite(float(minimum_autonomy_probability))
        and 0 <= calibrated_action_probability <= 1
        and 0 <= minimum_autonomy_probability <= 1
    )
    if action_type != "escalate" and (
        not confidence_valid
        or calibrated_action_probability < minimum_autonomy_probability
    ):
        if not confidence_valid:
            reason = (
                f"{severity.name}: uncalibrated remediation confidence "
                "cannot authorize mutation"
            )
        else:
            reason = (
                f"{severity.name}: calibrated remediation probability "
                f"{calibrated_action_probability:.3f} is below measured "
                f"threshold {minimum_autonomy_probability:.3f}"
            )
        return GateDecision(
            decision=AutonomyDecision.REQUIRES_APPROVAL,
            severity=severity,
            reversibility=reversibility,
            allowed_by_policy=True,
            reason=reason,
            confidence_calibrated=confidence_valid,
            calibrated_action_probability=calibrated_action_probability,
            minimum_autonomy_probability=minimum_autonomy_probability,
        )

    # 3. Reversibility floor.
    if reversibility is Reversibility.IRREVERSIBLE:
        decision = AutonomyDecision.REQUIRES_APPROVAL
        reason = f"{severity.name}: irreversible action always needs human approval"
    elif reversibility is Reversibility.RISKY:
        if low_sev and _has_rollback_plan(action):
            decision = AutonomyDecision.AUTONOMOUS
            reason = (
                f"{severity.name} (low) + risky action with rollback plan → autonomous"
            )
        else:
            missing = (
                "no rollback plan"
                if not _has_rollback_plan(action)
                else "high severity"
            )
            decision = AutonomyDecision.REQUIRES_APPROVAL
            reason = f"{severity.name}: risky action needs approval ({missing})"
    else:  # REVERSIBLE
        # 3. Severity gate.
        if low_sev:
            decision = AutonomyDecision.AUTONOMOUS
            reason = f"{severity.name} (low severity) + reversible action → autonomous"
        else:
            decision = AutonomyDecision.REQUIRES_APPROVAL
            reason = f"{severity.name} (high severity) → human approval required"

    logger.info(
        f"🛂 PolicyGate: {getattr(action, 'action_type', '?')} on "
        f"{getattr(action, 'target', '?')} → {decision.value} ({reason})"
    )
    return GateDecision(
        decision=decision,
        severity=severity,
        reversibility=reversibility,
        allowed_by_policy=True,
        reason=reason,
        confidence_calibrated=confidence_valid,
        calibrated_action_probability=calibrated_action_probability,
        minimum_autonomy_probability=minimum_autonomy_probability,
    )


def decide_plan(
    actions: List[Any],
    severity_assessment: SeverityAssessment,
    environment: str = "production",
    risk_score: float = 0.0,
    evaluate_fn: Optional[Callable[[Any, str, float], Tuple[bool, str]]] = None,
    calibrated_action_probability: Optional[float] = None,
    minimum_autonomy_probability: Optional[float] = None,
) -> Tuple[AutonomyDecision, List[GateDecision]]:
    """Decide a whole plan. The plan is only autonomous if *every* action is.

    Returns the aggregate decision and the per-action decisions. A single BLOCKED
    action blocks the plan; a single REQUIRES_APPROVAL downgrades it to approval.
    """
    per_action = [
        decide(
            a,
            severity_assessment,
            environment,
            risk_score,
            evaluate_fn,
            calibrated_action_probability,
            minimum_autonomy_probability,
        )
        for a in actions
    ]
    if any(d.decision is AutonomyDecision.BLOCKED for d in per_action):
        aggregate = AutonomyDecision.BLOCKED
    elif (
        all(d.decision is AutonomyDecision.AUTONOMOUS for d in per_action)
        and per_action
    ):
        aggregate = AutonomyDecision.AUTONOMOUS
    else:
        aggregate = AutonomyDecision.REQUIRES_APPROVAL

    logger.info(f"🛂 PolicyGate: plan of {len(actions)} action(s) → {aggregate.value}")
    return aggregate, per_action
