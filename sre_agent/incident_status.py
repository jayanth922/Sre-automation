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


def _mutations_executed(live_results: Any) -> bool:
    """Did anything that actually ran change the system?

    Reads and pages are EXECUTED like any other action, so counting entries
    says nothing about whether a remediation happened.
    """
    # Absolute, like `backend.models` above: this module is also loaded
    # straight from source (tests/test_incident_status.py) with no package.
    from sre_agent.executor import NON_MUTATING_ACTIONS

    for item in live_results or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status")) != "EXECUTED":
            continue
        if str(item.get("action_type", "")).lower() not in NON_MUTATING_ACTIONS:
            return True
    return False


def compute_incident_status(
    state: Any,
    report_payload: Any,
    verification_outcome: Any,
) -> IncidentStatus:
    """Derive the incident's status from the ACT report and the verification
    outcome. ``PENDING_ACKNOWLEDGMENT`` is returned when a plan was gated
    fully autonomous, executed, and live verification confirmed the fix —
    automated verification alone is not sufficient to call an incident
    ``RESOLVED``. Only a human acknowledging the fix (the "acknowledge"
    Slack command, routed through
    ``approval_flow.acknowledge_incident_resolution``) advances it to
    ``RESOLVED``. This function is meant to be the graph's only source of
    ``PENDING_ACKNOWLEDGMENT``.
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

    live_results = _get(report_payload, "live_results")
    if human_approved and not live_results:
        # Authorization was consumed, but no live action was applied (for
        # example EXECUTOR_LIVE is disabled or every action remained blocked).
        return IncidentStatus.INVESTIGATED

    if live_results and not _mutations_executed(live_results):
        # Everything that ran was a read or a page to a human. Verification is
        # deliberately skipped in that case (graph_builder only verifies after a
        # real mutation), so falling through to REMEDIATION_IN_PROGRESS told the
        # on-call a fix was landing when the agent had changed nothing and had
        # handed the incident to them. Nothing is in progress: what happened is
        # an investigation.
        return IncidentStatus.INVESTIGATED

    if verification_outcome is None:
        # Autonomous plan executed, but live verification hasn't run yet.
        return IncidentStatus.REMEDIATION_IN_PROGRESS

    outcome_status = str(_get(verification_outcome, "status", "") or "").upper()
    if outcome_status == "RESOLVED":
        return IncidentStatus.PENDING_ACKNOWLEDGMENT
    if outcome_status == "FAILED":
        return IncidentStatus.REMEDIATION_FAILED
    return IncidentStatus.VERIFICATION_UNKNOWN


def resolved_at_for_status(status: Any, now: Any) -> Any:
    """The ``resolved_at`` a row must carry alongside ``status`` — ``now`` or ``None``.

    Returned rather than merely "set it when resolved", because the timestamp
    has to follow the status in *both* directions. An incident can reach the
    end of a run already stamped resolved — an Alertmanager *resolved* webhook
    lands while the act phase is still verifying — and then compute
    REMEDIATION_FAILED. Writing only the status left rows reading
    ``remediation_failed`` with a ``resolved_at``, and MTTR
    (``api/v1/analytics.py``, ``api/v1/recommendations.py``) is measured as
    ``resolved_at - created_at`` filtered on ``resolved_at IS NOT NULL``: a
    failed remediation was being counted as a fast resolution.

    Note PENDING_ACKNOWLEDGMENT is *not* resolved. A verified fix still waits
    for a human to acknowledge it, and that path stamps the timestamp itself
    (``approval_flow.acknowledge_incident_resolution``).
    """
    return now if status == IncidentStatus.RESOLVED else None
