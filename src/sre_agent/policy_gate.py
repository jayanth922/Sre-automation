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
import re
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
    # Nothing was changed, so there is nothing to undo. Distinct from
    # REVERSIBLE, which asserts a mutation happened and can be walked back.
    READ_ONLY = "read_only"


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
    "inspect": Reversibility.READ_ONLY,  # reads config; mutates nothing
}

# Kept literal rather than imported from ``executor.READ_ONLY_ACTIONS`` so this
# module stays free of the executor's import chain, as the rest of it is
# deliberately dependency-light. ``tests/test_policy_gate.py`` asserts the two
# agree, so adding a read-only action in one place cannot silently skip the
# gate in the other.
_READ_ONLY_ACTION_TYPES: frozenset = frozenset({"inspect"})


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


# Only the diagnosis can say a fault is a leak: an OOM alert alone does not
# distinguish a leak from load-driven memory pressure, where scaling out is the
# right call. Reading the reflector's prose is safe here even though it is
# shaped by untrusted evidence, because this only ever *removes* options — an
# injected "memory leak" can at worst withhold a scale-out, never authorize one.
_LEAK_PATTERN = re.compile(
    r"\bmemory[- ]leak|\bleak(?:s|ing|ed)?\s+(?:memory|heap)|\bheap\s+leak"
    r"|\bunbounded\s+(?:memory|heap)\s+growth",
    re.IGNORECASE,
)


def diagnosed_memory_leak(*diagnoses: Optional[str]) -> bool:
    """True when any diagnosis text names a memory leak."""
    return any(_LEAK_PATTERN.search(str(text or "")) for text in diagnoses)


# An outage at a provider this service calls out to — the payment processor, a
# SaaS API — is fixed on the provider's side. Internal dependencies are
# deliberately not matched: when checkout fails because inventory-service is
# crash-looping, restarting inventory-service is the fix. Like the leak rule this
# only removes options, so a false match can at worst withhold a restart.
_DEPENDENCY_OUTAGE_PATTERN = re.compile(
    r"\b(?:external|third[- ]party|upstream\s+(?:payment\s+)?provider)\b"
    r"[^.;\n]{0,40}?\b(?:outage|failure|down|unavailable|unreachable)\b"
    r"|\bprovider\b[^.;\n]{0,25}?\b(?:outage|down|unavailable|unreachable)\b"
    r"|provider_down\b|provider_up\b[^.;\n]{0,20}?(?:=|to|at)\s*0\b",
    re.IGNORECASE,
)
# "not a provider outage", "rules out an external failure", "the provider is
# not down": a diagnosis that names the outage only to dismiss it.
_NEGATION = re.compile(
    r"\b(?:not|no|isn't|wasn't|never|without|unlikely|rather\s+than|instead\s+of"
    r"|rule[sd]?\s+out|ruling\s+out)\b",
    re.IGNORECASE,
)


def diagnosed_dependency_outage(*diagnoses: Optional[str]) -> bool:
    """True when any diagnosis text affirms an external provider outage."""
    for text in diagnoses:
        text = str(text or "")
        for match in _DEPENDENCY_OUTAGE_PATTERN.finditer(text):
            window = text[max(0, match.start() - 30) : match.end()]
            if not _NEGATION.search(window):
                return True
    return False


def unfit_remedy_reason(
    action: Any, memory_leak: bool, dependency_outage: bool = False
) -> Optional[str]:
    """Why an action cannot remediate the diagnosed fault, or None.

    This is a different question from reversibility or severity: a scale-out is
    perfectly safe and still does nothing for the pods that are leaking. Live on
    2026-09-29 (E2E Run 1, checkout memory leak) the planner proposed scale + a
    limit raise, omitted the restart its own OOM runbook recommends, and the
    gate offered both for approval — so a human approving "the fix" would have
    multiplied the leaking processes and postponed the OOM.

    Run 2 (payment provider outage) proposed restarting payment-service: the
    restart changed nothing at the provider, and only looked like a fix because
    the simulated outage lived in the restarted process.
    """
    action_type = str(getattr(action, "action_type", "")).lower()
    if dependency_outage and action_type in (
        "restart", "rollback", "revert_commit", "scale"
    ):
        # config_change stays open: failing over to a secondary provider is
        # a legitimate response to a provider outage.
        return (
            "cannot remediate an external provider outage: the fault is on the "
            "provider's side, and restarting, rolling back or scaling this "
            "service leaves the provider down"
        )
    if not memory_leak:
        return None
    if action_type == "scale":
        return (
            "does not remediate a memory leak: every replica leaks on its own, "
            "and adding replicas leaves the existing pods' memory where it is "
            "— a restart reclaims it"
        )
    params = getattr(action, "parameters", None) or {}
    if (
        action_type in ("config_change", "patch")
        and isinstance(params, dict)
        and params.get("memory")
    ):
        return (
            "does not remediate a memory leak: the working set grows past any "
            "limit, so raising it only postpones the next OOM"
        )
    return None


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
    action_type = str(getattr(action, "action_type", "")).lower()

    # 1b. A read-only action mutates nothing. No severity, telemetry gap or
    # calibration argument can make *looking* unsafe, and every gate below this
    # point reasons about the cost of a change that will not happen. Holding a
    # config dump for human approval is how diagnostics ended up disguised as
    # `config_change` in the first place: the planner had no read-only action
    # type to reach for, so inspection borrowed a mutation's risk profile and
    # burned an approval on a step that writes nothing. Hard policy above still
    # applies — a policy block stays final.
    if action_type in _READ_ONLY_ACTION_TYPES:
        return GateDecision(
            decision=AutonomyDecision.AUTONOMOUS,
            severity=severity,
            reversibility=reversibility,
            allowed_by_policy=True,
            reason=f"{severity.name}: read-only action mutates nothing → autonomous",
        )

    # 1c. Escalation pages a human and changes nothing else, so it is exactly
    # the action a high-severity or poorly-understood incident needs *sooner*.
    # Gating it behind the severity rule below inverted that: live on
    # 2026-09-29 (E2E Run 2, SEV1 payment provider outage) the page to on-call
    # waited on the same approval as the restart, so the human was asked to
    # authorize being told about the incident. Hard policy above still applies.
    if action_type == "escalate":
        return GateDecision(
            decision=AutonomyDecision.AUTONOMOUS,
            severity=severity,
            reversibility=reversibility,
            allowed_by_policy=True,
            reason=f"{severity.name}: notify-only escalation mutates nothing → autonomous",
        )

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

    # 2b. A rollback in production is never unattended.
    #
    # `rollback` is classified REVERSIBLE, so without this floor a low-severity
    # calibrated run would walk a production deployment back a revision with
    # nobody watching. This is the surviving half of the old
    # ``policy_engine`` Rule 4, moved here because "hold this for a human" is a
    # decision this gate can express and a ``(bool, reason)`` policy verdict
    # cannot — the old rule could only hard-block, which no human could appeal.
    if action_type == "rollback" and str(environment).lower() == "production":
        return GateDecision(
            decision=AutonomyDecision.REQUIRES_APPROVAL,
            severity=severity,
            reversibility=reversibility,
            allowed_by_policy=True,
            reason=f"{severity.name}: rollback in production always needs human approval",
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
