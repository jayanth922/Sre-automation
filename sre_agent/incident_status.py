#!/usr/bin/env python3
"""Single source of truth for deriving an incident's ``IncidentStatus``.

``agent_runtime.py`` previously hardcoded ``status=IncidentStatus.RESOLVED``
at the end of every successful graph run, regardless of whether a remediation
plan existed, whether it was auto-applied or held for human approval, or
whether the fix actually worked. ``compute_incident_status`` replaces that
assignment with a decision driven by the ACT report (``act_phase.ActReport``,
duck-typed dict-or-object) and the verification outcome oracle
(``sre_agent.verification.VerificationOutcome``, also duck-typed).

Pure and side-effect-free: no DB or LLM calls, so it's directly unit-testable.
"""

from __future__ import annotations

from typing import Any

from backend.models import IncidentStatus


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an attribute from an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def compute_incident_status(
    state: Any,
    report_payload: Any,
    verification_outcome: Any,
) -> IncidentStatus:
    """Derive the incident's status from the ACT report and the verification
    outcome. ``RESOLVED`` is returned only when a plan was gated fully
    autonomous, executed, and live verification confirmed the fix — this
    function is meant to be the graph's only source of ``RESOLVED``.
    """
    plan_present = bool(_get(report_payload, "plan_present", False))
    if not plan_present:
        # Investigation completed but there was nothing to remediate.
        return IncidentStatus.INVESTIGATED

    aggregate_decision = _get(report_payload, "aggregate_decision")
    approval = _get(report_payload, "approval", {}) or {}
    human_approved = _get(approval, "status") == "approved"
    if aggregate_decision != "autonomous" and not human_approved:
        # Any action requires approval, or the plan is fully/partially blocked.
        return IncidentStatus.AWAITING_APPROVAL

    if human_approved and not _get(report_payload, "live_results"):
        # Authorization was consumed, but no live action was applied (for
        # example EXECUTOR_LIVE is disabled or every action remained blocked).
        return IncidentStatus.INVESTIGATED

    if verification_outcome is None:
        # Autonomous plan executed, but live verification hasn't run yet.
        return IncidentStatus.REMEDIATION_IN_PROGRESS

    outcome_status = str(_get(verification_outcome, "status", "") or "").upper()
    if outcome_status == "RESOLVED":
        return IncidentStatus.RESOLVED
    if outcome_status == "FAILED":
        return IncidentStatus.REMEDIATION_FAILED
    return IncidentStatus.VERIFICATION_UNKNOWN
