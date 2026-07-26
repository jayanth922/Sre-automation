#!/usr/bin/env python3
"""
Severity Engine — automatic incident severity classification for the ACT phase.

This is the decision core that drives *severity-driven autonomy*: the agent may
remediate autonomously only when an incident is low severity; higher-severity
incidents require human approval.

How real products do it
-----------------------
The industry standard (ITIL, PagerDuty, incident.io) classifies severity as a
function of **impact × urgency**, bucketed into SEV levels:

- **Impact**  — potential business damage: blast radius, whether the affected
  service is user-facing / revenue-impacting, error magnitude, SLO breach.
- **Urgency** — how fast it is escalating: SLO burn rate, error-rate slope,
  saturation trend, whether it is still getting worse.

The key advantage here is that the SRE agent *already gathers every one of these
signals* while investigating (Golden Signals from Prometheus, K8s scope, SLO
state), so severity can be computed automatically instead of asked of a human.

Two safety rules, both taken from real practice, are baked in:

1. **"When unsure, round up."** incident.io's own guidance is to declare the
   *higher* severity when uncertain. So when the investigation's hypothesis
   confidence is low, we escalate severity by one level. Uncertainty defaults to
   *more* caution, never less.
2. Severity is only the *first* gate. The Policy Gate additionally applies a
   reversibility floor (see ``policy_gate.py``) so an irreversible action is
   never auto-executed even at low severity.

This module is pure logic (no LLM / infra imports) so it is fully unit-testable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal

logger = logging.getLogger(__name__)


class Severity(IntEnum):
    """Incident severity. Lower number = more critical (industry convention).

    SEV1 Critical  — core service down / broad user impact.
    SEV2 Major     — significant degradation, workaround may exist.
    SEV3 Moderate  — limited or contained impact.
    SEV4 Low       — minor / cosmetic / single-replica blip.
    """

    SEV1 = 1
    SEV2 = 2
    SEV3 = 3
    SEV4 = 4


Bucket = Literal["high", "medium", "low"]


@dataclass
class IncidentSignals:
    """Raw signals the investigation already produces, used to derive severity.

    Every field is optional so the engine degrades gracefully on partial data.
    Impact and urgency are computed from these; nothing here is guessed.
    """

    # ── Impact signals ──────────────────────────────────────────────
    affected_services: int = 1
    affected_pods: int = 0
    user_facing: bool = False
    revenue_impacting: bool = False
    error_rate: float = 0.0          # fraction of requests failing, 0.0–1.0
    slo_breached: bool = False

    # ── Urgency signals ─────────────────────────────────────────────
    slo_burn_rate: float = 0.0       # multiples of error-budget burn (14.4 = fast-burn)
    error_rate_slope: float = 0.0    # change in error_rate per minute
    saturation: float = 0.0          # resource saturation, 0.0–1.0
    still_escalating: bool = False

    # ── Meta ────────────────────────────────────────────────────────
    hypothesis_confidence: float = 1.0  # Reflector confidence, 0.0–1.0


@dataclass
class SeverityAssessment:
    """The result of a severity classification."""

    severity: Severity
    impact_score: float
    urgency_score: float
    impact_bucket: Bucket
    urgency_bucket: Bucket
    rounded_up: bool = False
    rationale: str = ""


# Normalization anchors (all env-overridable so the model can be tuned per tenant
# without touching code — a payment processor's SEV2 may be another team's SEV1).
_FAST_BURN_ANCHOR = float(os.getenv("SEVERITY_FAST_BURN_ANCHOR", "14.4"))  # SRE fast-burn multiple
_SLOPE_ANCHOR = float(os.getenv("SEVERITY_SLOPE_ANCHOR", "0.1"))           # error-rate rise/min
_BREADTH_ANCHOR = float(os.getenv("SEVERITY_BREADTH_ANCHOR", "5"))          # services for "wide"
_CONFIDENCE_ROUNDUP_THRESHOLD = float(os.getenv("SEVERITY_CONFIDENCE_ROUNDUP", "0.5"))

# Autonomy threshold: incidents at or *below* this criticality (i.e. numerically
# >= this SEV) are eligible for autonomous remediation. Default SEV3 → SEV3/SEV4
# may auto-remediate; SEV1/SEV2 require approval.
_AUTONOMY_MAX_SEVERITY = int(os.getenv("AUTONOMY_MAX_SEVERITY", str(int(Severity.SEV3))))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _bucket(score: float) -> Bucket:
    if score >= 0.66:
        return "high"
    if score >= 0.33:
        return "medium"
    return "low"


def compute_impact_score(s: IncidentSignals) -> float:
    """Impact in [0, 1]: how much business damage the incident can cause."""
    score = 0.0
    score += _clamp01(s.error_rate) * 0.40        # magnitude of failure
    score += 0.20 if s.slo_breached else 0.0      # objective breach
    score += 0.15 if s.user_facing else 0.0       # customers see it
    score += 0.15 if s.revenue_impacting else 0.0 # money at stake
    breadth = max(s.affected_services, 1) / _BREADTH_ANCHOR
    score += _clamp01(breadth) * 0.10             # blast radius
    return _clamp01(score)


def compute_urgency_score(s: IncidentSignals) -> float:
    """Urgency in [0, 1]: how fast the incident is escalating."""
    score = 0.0
    score += _clamp01(s.slo_burn_rate / _FAST_BURN_ANCHOR) * 0.40
    score += _clamp01(s.saturation) * 0.25
    score += _clamp01(s.error_rate_slope / _SLOPE_ANCHOR) * 0.20
    score += 0.15 if s.still_escalating else 0.0
    return _clamp01(score)


# Impact × urgency → severity matrix (ITIL-style). Rows = impact, cols = urgency.
_MATRIX: dict[tuple[Bucket, Bucket], Severity] = {
    ("high", "high"): Severity.SEV1,
    ("high", "medium"): Severity.SEV2,
    ("high", "low"): Severity.SEV3,
    ("medium", "high"): Severity.SEV2,
    ("medium", "medium"): Severity.SEV3,
    ("medium", "low"): Severity.SEV4,
    ("low", "high"): Severity.SEV3,
    ("low", "medium"): Severity.SEV4,
    ("low", "low"): Severity.SEV4,
}


def _escalate(sev: Severity) -> Severity:
    """Bump one level more critical, clamped at SEV1."""
    return Severity(max(int(Severity.SEV1), int(sev) - 1))


def classify_severity(signals: IncidentSignals) -> SeverityAssessment:
    """Classify an incident's severity from its signals.

    Pure function. Applies the impact×urgency matrix, then the
    "round up when unsure" safety rule based on hypothesis confidence.
    """
    impact = compute_impact_score(signals)
    urgency = compute_urgency_score(signals)
    ib, ub = _bucket(impact), _bucket(urgency)

    base = _MATRIX[(ib, ub)]

    rounded_up = False
    severity = base
    if signals.hypothesis_confidence < _CONFIDENCE_ROUNDUP_THRESHOLD:
        severity = _escalate(base)
        rounded_up = severity != base

    rationale = (
        f"impact={impact:.2f}({ib}) × urgency={urgency:.2f}({ub}) → {base.name}"
    )
    if rounded_up:
        rationale += (
            f"; escalated to {severity.name} "
            f"(confidence {signals.hypothesis_confidence:.2f} < {_CONFIDENCE_ROUNDUP_THRESHOLD})"
        )

    logger.info(f"🎚️  SeverityEngine: {rationale}")

    return SeverityAssessment(
        severity=severity,
        impact_score=impact,
        urgency_score=urgency,
        impact_bucket=ib,
        urgency_bucket=ub,
        rounded_up=rounded_up,
        rationale=rationale,
    )


def is_low_severity(severity: Severity) -> bool:
    """Is this severity within the autonomous-remediation band?

    Returns True for incidents the agent may remediate without human approval
    (subject to the Policy Gate's reversibility floor).
    """
    return int(severity) >= _AUTONOMY_MAX_SEVERITY
