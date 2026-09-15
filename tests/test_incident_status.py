#!/usr/bin/env python3
"""Table-driven tests for `compute_incident_status` (PR-T01).

Covers every branch of the decision function that replaced the old
unconditional `status=IncidentStatus.RESOLVED` assignment in
`agent_runtime.py`'s SaaS background-execution success path.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# `backend.models` only needs `IncidentStatus`, a plain `str` Enum with no
# sqlalchemy/fastapi dependency chain, but importing the real `backend.models`
# module pulls those in anyway. Load it directly from source, same pattern as
# tests/test_checkpointer.py uses for sre_agent/checkpointer.py.
_models = _load("backend_models_for_incident_status_test", "backend/models.py")
IncidentStatus = _models.IncidentStatus

_incident_status = _load("incident_status_under_test", "sre_agent/incident_status.py")
compute_incident_status = _incident_status.compute_incident_status
effective_status_after_run = _incident_status.effective_status_after_run
resolved_at_for_status = _incident_status.resolved_at_for_status


TABLE = [
    pytest.param(
        {"plan_present": False},
        None,
        IncidentStatus.INVESTIGATED,
        id="no-plan",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "requires_approval"},
        None,
        IncidentStatus.AWAITING_APPROVAL,
        id="plan-non-autonomous",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "blocked"},
        None,
        IncidentStatus.AWAITING_APPROVAL,
        id="plan-blocked",
    ),
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
        },
        None,
        IncidentStatus.INVESTIGATED,
        id="approved-but-live-execution-disabled",
    ),
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
            "live_results": [{"status": "EXECUTED"}],
        },
        None,
        IncidentStatus.REMEDIATION_IN_PROGRESS,
        id="approved-and-executed-awaiting-verification",
    ),
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
            "live_results": [{"status": "EXECUTED"}],
        },
        {"status": "resolved"},
        IncidentStatus.PENDING_ACKNOWLEDGMENT,
        id="approved-executed-and-verified",
    ),
    # A plan of reads and pages executes cleanly and mutates nothing, so
    # verification never runs. "Remediation in progress" would then sit on the
    # incident forever, claiming a fix was landing while the agent had handed
    # the problem to a human.
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
            "live_results": [
                {"status": "EXECUTED", "action_type": "inspect"},
                {"status": "EXECUTED", "action_type": "escalate"},
            ],
        },
        None,
        IncidentStatus.INVESTIGATED,
        id="approved-but-only-reads-and-pages-ran",
    ),
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
            "live_results": [
                {"status": "EXECUTED", "action_type": "escalate"},
                {"status": "FAILED", "action_type": "patch_deployment_env"},
            ],
        },
        None,
        IncidentStatus.INVESTIGATED,
        id="the-only-mutation-failed-so-nothing-is-in-progress",
    ),
    pytest.param(
        {
            "plan_present": True,
            "aggregate_decision": "requires_approval",
            "approval": {"status": "approved"},
            "live_results": [
                {"status": "EXECUTED", "action_type": "escalate"},
                {"status": "EXECUTED", "action_type": "patch_deployment_env"},
            ],
        },
        None,
        IncidentStatus.REMEDIATION_IN_PROGRESS,
        id="one-real-mutation-among-pages-is-still-a-remediation",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        None,
        IncidentStatus.REMEDIATION_IN_PROGRESS,
        id="autonomous-no-verification-yet",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {"status": "resolved"},
        IncidentStatus.PENDING_ACKNOWLEDGMENT,
        id="autonomous-verification-resolved",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {"status": "RESOLVED"},
        IncidentStatus.PENDING_ACKNOWLEDGMENT,
        id="autonomous-verification-resolved-uppercase",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {"status": "failed"},
        IncidentStatus.REMEDIATION_FAILED,
        id="autonomous-verification-failed",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {"status": "inconclusive"},
        IncidentStatus.VERIFICATION_UNKNOWN,
        id="autonomous-verification-unknown-status",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {},
        IncidentStatus.VERIFICATION_UNKNOWN,
        id="autonomous-verification-outcome-missing-status-key",
    ),
]


@pytest.mark.parametrize("report_payload, verification_outcome, expected", TABLE)
def test_compute_incident_status(report_payload, verification_outcome, expected):
    result = compute_incident_status(
        state={}, report_payload=report_payload, verification_outcome=verification_outcome
    )
    assert result == expected


@pytest.mark.parametrize("report_payload, verification_outcome, expected", TABLE)
def test_resolved_at_set_iff_resolved(report_payload, verification_outcome, expected):
    """`resolved_at` is stamped exactly when the computed status is RESOLVED.

    Calls the helper `agent_runtime` actually uses rather than restating the
    rule, so the test fails if the write and the decision drift apart.
    """
    computed_status = compute_incident_status(
        state={}, report_payload=report_payload, verification_outcome=verification_outcome
    )
    now = object()
    stamped = resolved_at_for_status(computed_status, now)
    assert (stamped is now) == (expected == IncidentStatus.RESOLVED)


def test_external_resolution_wins_over_a_later_graph_status():
    """A finishing investigation may enrich findings, but cannot reopen a
    source alert that Alertmanager has already reported as recovered."""
    assert (
        effective_status_after_run(
            IncidentStatus.RESOLVED, IncidentStatus.REMEDIATION_FAILED
        )
        == IncidentStatus.RESOLVED
    )


def test_unresolved_incident_still_uses_the_graphs_computed_status():
    assert (
        effective_status_after_run(
            IncidentStatus.INVESTIGATING, IncidentStatus.REMEDIATION_FAILED
        )
        == IncidentStatus.REMEDIATION_FAILED
    )


def test_awaiting_a_humans_acknowledgment_is_not_yet_resolved():
    """A verified fix still needs the on-call to acknowledge it; the
    acknowledge path stamps the timestamp itself."""
    assert resolved_at_for_status(IncidentStatus.PENDING_ACKNOWLEDGMENT, object()) is None


def test_object_style_report_and_outcome_are_duck_typed():
    """report_payload/verification_outcome may be attribute-bearing objects
    (e.g. dataclasses/pydantic models), not just dicts."""

    class Report:
        plan_present = True
        aggregate_decision = "autonomous"

    class Outcome:
        status = "resolved"

    assert (
        compute_incident_status(state={}, report_payload=Report(), verification_outcome=Outcome())
        == IncidentStatus.PENDING_ACKNOWLEDGMENT
    )


def test_every_terminal_status_writer_stamps_resolved_at_through_the_helper():
    """Three modules end a run by writing the computed status to the incident
    row — the autonomous path (`agent_runtime`), the Slack approval path
    (`approval_flow`), and the dashboard approval path (`mission_control`).
    The first was fixed alone, and the other two kept the
    `if status == RESOLVED: stamp` shape, so a run approved from Slack still
    left `remediation_failed` rows carrying a `resolved_at`. Whoever computes
    the status owns the timestamp that goes with it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    writers = [
        root / "sre_agent" / "agent_runtime.py",
        root / "sre_agent" / "approval_flow.py",
        root / "sre_agent" / "api" / "v1" / "mission_control.py",
    ]
    for path in writers:
        source = path.read_text()
        assert "compute_incident_status" in source, f"{path.name} is no longer a writer"
        assert "resolved_at_for_status" in source, (
            f"{path.name} computes an incident status but stamps resolved_at "
            "itself; use incident_status.resolved_at_for_status so a failed "
            "remediation cannot keep an earlier resolved timestamp"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
