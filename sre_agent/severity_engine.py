#!/usr/bin/env python3
"""
Severity Engine — automatic incident severity classification for the ACT phase.

Severity is derived only from **measured** evidence (SLO burn, error rate,
blast radius, duration, customer scope, dependencies, confidence). Missing
telemetry stays UNKNOWN and forces escalation — it is never invented from
alert severity labels or specialist-result key counts.

This module is pure logic (no LLM / infra imports) so it is fully unit-testable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


class Severity(IntEnum):
    """Incident severity. Lower number = more critical (industry convention).

    UNKNOWN     — required telemetry missing; never treat as autonomous.
    SEV1 Critical  — core service down / broad user impact.
    SEV2 Major     — significant degradation, workaround may exist.
    SEV3 Moderate  — limited or contained impact.
    SEV4 Low       — minor / cosmetic / single-replica blip.
    """

    UNKNOWN = 0
    SEV1 = 1
    SEV2 = 2
    SEV3 = 3
    SEV4 = 4


Bucket = Literal["high", "medium", "low", "unknown"]


@dataclass(frozen=True)
class EvidenceLink:
    """One severity feature tied to a concrete observation."""

    field: str
    value: Any
    source: str
    observed_at: str
    unknown: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "unknown": self.unknown,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence(
    field: str,
    value: Any,
    source: str,
    *,
    observed_at: Optional[str] = None,
    unknown: bool = False,
) -> EvidenceLink:
    return EvidenceLink(
        field=field,
        value=value,
        source=source,
        observed_at=observed_at or _now_iso(),
        unknown=unknown,
    )


@dataclass
class IncidentSignals:
    """Measured incident features. Optional numeric fields mean UNKNOWN.

    Do not default measured rates to 0.0 — that fabricates a calm signal.
    """

    # ── Impact signals ──────────────────────────────────────────────
    affected_services: Optional[int] = None
    affected_pods: Optional[int] = None
    user_facing: Optional[bool] = None
    revenue_impacting: Optional[bool] = None
    error_rate: Optional[float] = None  # fraction of requests failing, 0.0–1.0
    slo_breached: Optional[bool] = None
    customer_scope: Optional[str] = None  # e.g. single_tenant | multi_tenant | unknown
    dependency_count: Optional[int] = None
    duration_seconds: Optional[float] = None

    # ── Urgency signals ─────────────────────────────────────────────
    slo_burn_rate: Optional[float] = None  # multiples of error-budget burn
    error_rate_slope: Optional[float] = None  # change in error_rate per minute
    saturation: Optional[float] = None  # resource saturation, 0.0–1.0
    still_escalating: Optional[bool] = None

    # ── Meta ────────────────────────────────────────────────────────
    hypothesis_confidence: Optional[float] = None  # Reflector confidence
    evidence: List[EvidenceLink] = field(default_factory=list)

    def unknown_fields(self) -> List[str]:
        measured = (
            "affected_services",
            "error_rate",
            "slo_burn_rate",
            "slo_breached",
            "saturation",
            "hypothesis_confidence",
        )
        return [name for name in measured if getattr(self, name) is None]


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
    unknown_telemetry: bool = False
    evidence: List[EvidenceLink] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.name,
            "impact_score": self.impact_score,
            "urgency_score": self.urgency_score,
            "impact_bucket": self.impact_bucket,
            "urgency_bucket": self.urgency_bucket,
            "rounded_up": self.rounded_up,
            "rationale": self.rationale,
            "unknown_telemetry": self.unknown_telemetry,
            "evidence": [item.to_dict() for item in self.evidence],
        }


# Normalization anchors (env-overridable).
_FAST_BURN_ANCHOR = float(os.getenv("SEVERITY_FAST_BURN_ANCHOR", "14.4"))
_SLOPE_ANCHOR = float(os.getenv("SEVERITY_SLOPE_ANCHOR", "0.1"))
_BREADTH_ANCHOR = float(os.getenv("SEVERITY_BREADTH_ANCHOR", "5"))
_CONFIDENCE_ROUNDUP_THRESHOLD = float(os.getenv("SEVERITY_CONFIDENCE_ROUNDUP", "0.5"))
_AUTONOMY_MAX_SEVERITY = int(
    os.getenv("AUTONOMY_MAX_SEVERITY", str(int(Severity.SEV3)))
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _bucket(score: Optional[float], *, unknown: bool = False) -> Bucket:
    if unknown or score is None:
        return "unknown"
    if score >= 0.66:
        return "high"
    if score >= 0.33:
        return "medium"
    return "low"


def compute_impact_score(s: IncidentSignals) -> tuple[Optional[float], bool]:
    """Impact in [0, 1], or (None, True) when required measurements are missing."""
    if s.error_rate is None and s.affected_services is None and s.slo_breached is None:
        return None, True

    score = 0.0
    unknown = False
    if s.error_rate is None:
        unknown = True
    else:
        score += _clamp01(s.error_rate) * 0.40
    if s.slo_breached is None:
        unknown = True
    elif s.slo_breached:
        score += 0.20
    if s.user_facing:
        score += 0.15
    if s.revenue_impacting:
        score += 0.15
    if s.affected_services is None:
        unknown = True
    else:
        breadth = max(s.affected_services, 0) / _BREADTH_ANCHOR
        score += _clamp01(breadth) * 0.10
    if s.dependency_count:
        score += _clamp01(s.dependency_count / _BREADTH_ANCHOR) * 0.05
    return _clamp01(score), unknown


def compute_urgency_score(s: IncidentSignals) -> tuple[Optional[float], bool]:
    """Urgency in [0, 1], or unknown when burn/slope/saturation are missing."""
    if s.slo_burn_rate is None and s.saturation is None and s.error_rate_slope is None:
        return None, True

    score = 0.0
    unknown = False
    if s.slo_burn_rate is None:
        unknown = True
    else:
        score += _clamp01(s.slo_burn_rate / _FAST_BURN_ANCHOR) * 0.40
    if s.saturation is None:
        unknown = True
    else:
        score += _clamp01(s.saturation) * 0.25
    if s.error_rate_slope is None:
        unknown = True
    else:
        score += _clamp01(s.error_rate_slope / _SLOPE_ANCHOR) * 0.20
    if s.still_escalating:
        score += 0.15
    if s.duration_seconds is not None and s.duration_seconds >= 900:
        score += 0.10
    return _clamp01(score), unknown


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
    if sev is Severity.UNKNOWN:
        return Severity.SEV2
    return Severity(max(int(Severity.SEV1), int(sev) - 1))


def classify_severity(signals: IncidentSignals) -> SeverityAssessment:
    """Classify severity from measured signals only.

    Missing required telemetry → UNKNOWN or escalated severity that requires
    human approval — never fabricated rates from alert labels.
    """
    impact, impact_unknown = compute_impact_score(signals)
    urgency, urgency_unknown = compute_urgency_score(signals)
    unknown_telemetry = (
        impact_unknown or urgency_unknown or bool(signals.unknown_fields())
    )

    if impact is None or urgency is None:
        rationale = (
            "unknown telemetry: required impact/urgency measurements missing; "
            "escalating to UNKNOWN (no fabricated severity)"
        )
        logger.info("🎚️  SeverityEngine: %s", rationale)
        return SeverityAssessment(
            severity=Severity.UNKNOWN,
            impact_score=0.0,
            urgency_score=0.0,
            impact_bucket="unknown",
            urgency_bucket="unknown",
            rounded_up=True,
            rationale=rationale,
            unknown_telemetry=True,
            evidence=list(signals.evidence),
        )

    ib, ub = _bucket(impact, unknown=impact_unknown), _bucket(
        urgency, unknown=urgency_unknown
    )
    if ib == "unknown" or ub == "unknown":
        # Partial scores exist but critical dimensions are unknown → escalate.
        base = Severity.SEV2
        rationale = (
            f"impact={impact:.2f}({ib}) × urgency={urgency:.2f}({ub}) → "
            f"{base.name} (unknown telemetry escalation)"
        )
        logger.info("🎚️  SeverityEngine: %s", rationale)
        return SeverityAssessment(
            severity=base,
            impact_score=impact,
            urgency_score=urgency,
            impact_bucket=ib,
            urgency_bucket=ub,
            rounded_up=True,
            rationale=rationale,
            unknown_telemetry=True,
            evidence=list(signals.evidence),
        )

    base = _MATRIX[(ib, ub)]
    rounded_up = False
    severity = base
    confidence = signals.hypothesis_confidence
    if confidence is None or confidence < _CONFIDENCE_ROUNDUP_THRESHOLD:
        severity = _escalate(base)
        rounded_up = severity != base

    rationale = f"impact={impact:.2f}({ib}) × urgency={urgency:.2f}({ub}) → {base.name}"
    if rounded_up:
        conf_txt = "missing" if confidence is None else f"{confidence:.2f}"
        rationale += (
            f"; escalated to {severity.name} "
            f"(confidence {conf_txt} < {_CONFIDENCE_ROUNDUP_THRESHOLD})"
        )
    if unknown_telemetry:
        rationale += "; partial unknown fields present"

    logger.info("🎚️  SeverityEngine: %s", rationale)

    return SeverityAssessment(
        severity=severity,
        impact_score=impact,
        urgency_score=urgency,
        impact_bucket=ib,
        urgency_bucket=ub,
        rounded_up=rounded_up,
        rationale=rationale,
        unknown_telemetry=unknown_telemetry,
        evidence=list(signals.evidence),
    )


def is_low_severity(severity: Severity) -> bool:
    """Is this severity within the autonomous-remediation band?

    UNKNOWN is never autonomous.
    """
    if severity is Severity.UNKNOWN or int(severity) == 0:
        return False
    return int(severity) >= _AUTONOMY_MAX_SEVERITY
