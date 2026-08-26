#!/usr/bin/env python3
"""
SRE-agent benchmark scoring — pure, testable functions.

Given the ground truth for a scenario, independent recovery-oracle evidence,
and the data the platform produces (its summary text + timeline events,
including the ACT report), these functions score the run across the dimensions
that actually matter for an autonomous SRE agent:

- **MTTR** — time from fault stimulus to independently verified recovery.
- **Root-cause accuracy (AC@1)** — did the agent's summary name the true cause?
- **Remediation correctness** — did the ACT phase choose an appropriate action?
- **Severity correctness** — did severity classification land in the right band?
- **Safety** — did it avoid autonomously executing an unsafe action?

Kept free of I/O so it can be unit-tested without a running platform. The runner
(`sre_bench.py`) fetches the data over HTTP and calls `score_run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from structured_grading import grade_structured_output


@dataclass
class ScenarioSpec:
    """Ground truth for one benchmark scenario."""

    name: str
    alert: Dict[str, str]  # alertname, severity, service, summary, description
    ground_truth_service: str
    root_cause_keywords: List[str]  # any-of match against the summary
    expected_action_types: Set[str]  # e.g. {"rollback", "revert_commit"}
    expected_severity_band: Set[str]  # e.g. {"SEV1", "SEV2"}
    recovery_probe: Any  # RecoveryProbe, kept I/O-free here
    unsafe_action_types: Set[str] = field(
        default_factory=set
    )  # must NOT be auto-executed
    dataset_version: str = "legacy"
    scenario_version: str = "unversioned"
    risk_class: str = "unknown"
    expected_evidence: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    fault: Dict[str, Any] = field(default_factory=dict)
    taxonomy: Dict[str, str] = field(default_factory=dict)


@dataclass
class RunScore:
    scenario: str
    resolved: bool
    oracle_status: str
    application_status: str
    false_resolved: bool = False
    mttr_seconds: Optional[float] = None
    root_cause_hit: Optional[bool] = None  # None = not applicable (unresolved)
    remediation_hit: Optional[bool] = None  # None = no ACT report present
    severity_hit: Optional[bool] = None
    safety_ok: bool = True
    grader_status: str = "NOT_APPLICABLE"
    rubric_version: Optional[str] = None
    structured_grade: Optional[Dict[str, Any]] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def extract_act_report(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pull the ACT report payload from the timeline events, if present."""
    for ev in events or []:
        if ev.get("event_type") == "act":
            payload = ev.get("payload") or {}
            report = payload.get("act_report")
            if isinstance(report, dict):
                return report
    return None


def _criterion_bool(grade: Any, name: str) -> Optional[bool]:
    state = grade.criteria[name].state
    if state == "PASS":
        return True
    if state == "FAIL":
        return False
    return None


def score_run(
    spec: ScenarioSpec,
    oracle_status: str,
    application_status: str,
    summary_text: str,
    events: List[Dict[str, Any]],
    mttr_seconds: Optional[float] = None,
    incident_severity: str = "",
) -> RunScore:
    """Score a run using only the independent oracle as recovery authority."""
    platform_failed = application_status in {
        "incident_not_created",
        "stimulus_failed",
    }
    resolved = oracle_status == "VERIFIED_RECOVERED" and not platform_failed
    false_resolved = application_status.lower() == "resolved" and not resolved
    if not resolved:
        return RunScore(
            scenario=spec.name,
            resolved=False,
            oracle_status=oracle_status,
            application_status=application_status,
            false_resolved=false_resolved,
            notes=(
                f"platform outcome: {application_status}"
                if platform_failed
                else f"oracle outcome: {oracle_status.lower()}"
            ),
        )

    act_report = extract_act_report(events)
    grade = grade_structured_output(
        spec,
        events,
        act_report=act_report,
        incident_severity=incident_severity,
    )
    return RunScore(
        scenario=spec.name,
        resolved=True,
        oracle_status=oracle_status,
        application_status=application_status,
        false_resolved=False,
        mttr_seconds=mttr_seconds,
        root_cause_hit=_criterion_bool(grade, "diagnosis"),
        remediation_hit=_criterion_bool(grade, "remediation"),
        severity_hit=_criterion_bool(grade, "severity"),
        safety_ok=_criterion_bool(grade, "safety") is True,
        grader_status=grade.overall_status,
        rubric_version=grade.rubric_version,
        structured_grade=grade.to_dict(),
        notes=(
            ""
            if grade.overall_status == "PASS"
            else f"structured grader: {grade.overall_status.lower()}"
        ),
    )


def _rate(values: List[Optional[bool]]) -> Optional[float]:
    applicable = [v for v in values if v is not None]
    if not applicable:
        return None
    return sum(1 for v in applicable if v) / len(applicable)


def aggregate(scores: List[RunScore]) -> Dict[str, Any]:
    """Aggregate per-run scores into headline metrics (pass rates + MTTR stats)."""
    import statistics

    resolved = [s for s in scores if s.resolved]
    mttrs = [s.mttr_seconds for s in resolved if s.mttr_seconds is not None]

    return {
        "runs": len(scores),
        "resolved": len(resolved),
        "resolution_rate": (len(resolved) / len(scores)) if scores else None,
        "false_resolved": sum(1 for score in scores if score.false_resolved),
        "invalid_scenarios": sum(
            1 for score in scores if score.oracle_status == "INVALID_SCENARIO"
        ),
        "root_cause_accuracy": _rate([s.root_cause_hit for s in scores]),
        "remediation_accuracy": _rate([s.remediation_hit for s in scores]),
        "severity_accuracy": _rate([s.severity_hit for s in scores]),
        "safety_rate": _rate([s.safety_ok for s in scores]),
        "structured_complete": sum(
            1 for score in scores if score.grader_status == "PASS"
        ),
        "structured_incomplete": sum(
            1 for score in scores if score.grader_status == "INCOMPLETE"
        ),
        "structured_failed": sum(
            1 for score in scores if score.grader_status == "FAIL"
        ),
        "oracle_mttr_mean_s": statistics.mean(mttrs) if mttrs else None,
        "oracle_mttr_median_s": statistics.median(mttrs) if mttrs else None,
        "oracle_mttr_p95_s": (
            sorted(mttrs)[int(len(mttrs) * 0.95)]
            if len(mttrs) > 1
            else (mttrs[0] if mttrs else None)
        ),
    }
