#!/usr/bin/env python3
"""A10 verified-only learning: promote successful exemplars only after objective recovery.

Blocked, dry-run, failed, and unknown outcomes never become successful memory,
skills, or runbooks. Negative examples are retained with provenance so the system
can learn what not to repeat, and artifacts can be invalidated when evidence
reverses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

SCHEMA_VERSION = 1
OutcomeClass = Literal[
    "verified_success",
    "dry_run",
    "blocked",
    "failed",
    "unknown",
    "incomplete",
]
ArtifactKind = Literal["memory", "skill", "runbook"]
_SUCCESS_STATUSES = {"EXECUTED", "OK", "SUCCESS", "REVERT_REQUESTED"}


class VerifiedLearningError(ValueError):
    """Learning evidence is incomplete or cannot authorize promotion."""


@dataclass(frozen=True)
class LearningEligibility:
    eligible_for_success: bool
    outcome_class: OutcomeClass
    reasons: tuple[str, ...]
    verification_status: Optional[str]
    incident_status: Optional[str]
    live_executed_count: int
    dry_run_only: bool
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LearningProvenance:
    incident_id: str
    verification_status: str
    outcome_class: OutcomeClass
    artifact_kind: ArtifactKind
    reviewer_id: Optional[str] = None
    run_manifest_sha256: Optional[str] = None
    config_fingerprint: Optional[str] = None
    oracle_probe_sha256: Optional[str] = None
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["recorded_at"] = self.recorded_at.isoformat()
        return value


@dataclass(frozen=True)
class NegativeExemplar:
    exemplar_id: str
    outcome_class: OutcomeClass
    incident_id: str
    summary: str
    actions: tuple[dict[str, Any], ...]
    provenance: LearningProvenance
    invalidated: bool = False
    invalidation_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actions"] = list(self.actions)
        value["provenance"] = self.provenance.to_dict()
        return value


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _status(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(_get(value, "status", value) or "").strip().upper()
    return text or None


def _list_actions(*groups: Any) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        values = group if isinstance(group, list) else [group]
        for item in values:
            if isinstance(item, dict):
                actions.append(item)
            elif item is not None:
                actions.append(
                    {
                        "action_type": str(_get(item, "action_type", "")),
                        "target": str(_get(item, "target", "")),
                        "status": str(_get(item, "status", "")),
                    }
                )
    return actions


def live_executed_actions(
    live_results: Any = None,
    executed: Any = None,
) -> list[dict[str, Any]]:
    """Return only actions that actually mutated (or succeeded live), not dry-runs."""
    selected: list[dict[str, Any]] = []
    for action in _list_actions(live_results, executed):
        status = str(action.get("status", "") or "").upper()
        mode = str(action.get("mode", "") or "").lower()
        if mode in {"dry_run", "dry-run"}:
            continue
        if status == "DRY_RUN":
            continue
        if status in _SUCCESS_STATUSES:
            selected.append(action)
    return selected


def assess_learning_eligibility(
    *,
    act_report: Any = None,
    verification_outcome: Any = None,
    incident_status: Any = None,
    live_results: Any = None,
    executed: Any = None,
) -> LearningEligibility:
    """Decide whether an investigation may become a successful exemplar."""
    reasons: list[str] = []
    verification = _status(verification_outcome)
    if verification is None and act_report is not None:
        verification = _status(_get(act_report, "verification"))
    if live_results is None and act_report is not None:
        live_results = _get(act_report, "live_results")
    if executed is None and act_report is not None:
        executed = _get(act_report, "executed")

    live = live_executed_actions(live_results=live_results, executed=executed)
    dry_run_candidates = [
        action
        for action in _list_actions(executed, live_results)
        if str(action.get("status", "")).upper() == "DRY_RUN"
        or str(action.get("mode", "")).lower() in {"dry_run", "dry-run"}
        or (
            not str(action.get("status", "")).strip()
            and action in _list_actions(executed)
            and not live
        )
    ]
    aggregate = str(_get(act_report, "aggregate_decision", "") or "").lower()
    approval = _get(act_report, "approval", {}) or {}
    human_approved = _get(approval, "status") == "approved"
    plan_present = bool(_get(act_report, "plan_present", True))
    status_name = None
    if incident_status is not None:
        status_name = str(
            getattr(incident_status, "value", incident_status) or ""
        ).upper()

    if not plan_present and verification != "RESOLVED":
        reasons.append("no remediation plan was present")
        outcome: OutcomeClass = "incomplete"
    elif aggregate in {"blocked", "block"} and not human_approved:
        reasons.append("plan was blocked without human approval")
        outcome = "blocked"
    elif dry_run_candidates and not live:
        reasons.append("only dry-run actions were observed")
        outcome = "dry_run"
    elif verification is None:
        reasons.append("objective verification is missing")
        outcome = "incomplete"
    elif verification == "FAILED":
        reasons.append("verification reported FAILED")
        outcome = "failed"
    elif verification in {"UNKNOWN", "VERIFICATION_UNKNOWN"}:
        reasons.append("verification reported UNKNOWN")
        outcome = "unknown"
    elif verification != "RESOLVED":
        reasons.append(f"verification status {verification} is not RESOLVED")
        outcome = "unknown"
    elif not live:
        reasons.append("no live executed remediation action is present")
        outcome = "incomplete"
    elif status_name and status_name not in {"RESOLVED", ""}:
        reasons.append(f"incident status {status_name} is not RESOLVED")
        outcome = "incomplete"
    else:
        outcome = "verified_success"

    if outcome != "verified_success" and not reasons:
        reasons.append(f"outcome class {outcome} cannot promote successful exemplars")

    return LearningEligibility(
        eligible_for_success=outcome == "verified_success",
        outcome_class=outcome,
        reasons=tuple(reasons),
        verification_status=verification,
        incident_status=status_name,
        live_executed_count=len(live),
        dry_run_only=bool(dry_run_candidates) and not live,
    )


def build_provenance(
    *,
    incident_id: Optional[str],
    eligibility: LearningEligibility,
    artifact_kind: ArtifactKind,
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    oracle_probe_sha256: Optional[str] = None,
) -> LearningProvenance:
    if not incident_id or not str(incident_id).strip():
        raise VerifiedLearningError("learned artifacts require an incident_id")
    if (
        eligibility.eligible_for_success
        and eligibility.verification_status != "RESOLVED"
    ):
        raise VerifiedLearningError(
            "successful exemplars require RESOLVED verification"
        )
    return LearningProvenance(
        incident_id=str(incident_id).strip(),
        verification_status=eligibility.verification_status or "MISSING",
        outcome_class=eligibility.outcome_class,
        artifact_kind=artifact_kind,
        reviewer_id=reviewer_id,
        run_manifest_sha256=run_manifest_sha256,
        config_fingerprint=config_fingerprint,
        oracle_probe_sha256=oracle_probe_sha256,
    )


def negative_exemplar_id(
    *,
    incident_id: str,
    outcome_class: OutcomeClass,
    actions: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "incident_id": incident_id,
                "outcome_class": outcome_class,
                "actions": actions,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"neg-{outcome_class}-{digest[:16]}"


def build_negative_exemplar(
    *,
    eligibility: LearningEligibility,
    incident_id: Optional[str],
    summary: str,
    actions: list[dict[str, Any]],
    reviewer_id: Optional[str] = None,
    run_manifest_sha256: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> NegativeExemplar:
    if eligibility.eligible_for_success:
        raise VerifiedLearningError("verified success is not a negative exemplar")
    if not incident_id or not str(incident_id).strip():
        raise VerifiedLearningError("negative exemplars require an incident_id")
    provenance = build_provenance(
        incident_id=incident_id,
        eligibility=eligibility,
        artifact_kind="skill",
        reviewer_id=reviewer_id,
        run_manifest_sha256=run_manifest_sha256,
        config_fingerprint=config_fingerprint,
    )
    return NegativeExemplar(
        exemplar_id=negative_exemplar_id(
            incident_id=str(incident_id),
            outcome_class=eligibility.outcome_class,
            actions=actions,
        ),
        outcome_class=eligibility.outcome_class,
        incident_id=str(incident_id),
        summary=summary or eligibility.outcome_class,
        actions=tuple(actions),
        provenance=provenance,
    )


def memory_metadata_for_promotion(
    *,
    eligibility: LearningEligibility,
    provenance: LearningProvenance,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not eligibility.eligible_for_success:
        raise VerifiedLearningError(
            "blocked/dry-run/failed/unknown outcomes cannot become successful memory"
        )
    payload = dict(extra or {})
    payload.update(
        {
            "learning_schema_version": SCHEMA_VERSION,
            "learning_outcome": "verified_success",
            "verification_status": provenance.verification_status,
            "incident_id": provenance.incident_id,
            "reviewer_id": provenance.reviewer_id,
            "run_manifest_sha256": provenance.run_manifest_sha256,
            "config_fingerprint": provenance.config_fingerprint,
            "oracle_probe_sha256": provenance.oracle_probe_sha256,
            "promoted_at": provenance.recorded_at.isoformat(),
        }
    )
    return payload
