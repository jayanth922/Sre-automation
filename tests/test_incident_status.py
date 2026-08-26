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
        IncidentStatus.RESOLVED,
        id="approved-executed-and-verified",
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
        IncidentStatus.RESOLVED,
        id="autonomous-verification-resolved",
    ),
    pytest.param(
        {"plan_present": True, "aggregate_decision": "autonomous"},
        {"status": "RESOLVED"},
        IncidentStatus.RESOLVED,
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
    """Mirrors the guard in agent_runtime.py: `resolved_at` must be set
    exactly when the computed status is RESOLVED, never otherwise."""
    computed_status = compute_incident_status(
        state={}, report_payload=report_payload, verification_outcome=verification_outcome
    )
    should_set_resolved_at = computed_status == IncidentStatus.RESOLVED
    assert should_set_resolved_at == (expected == IncidentStatus.RESOLVED)


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
        == IncidentStatus.RESOLVED
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
