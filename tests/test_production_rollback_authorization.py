#!/usr/bin/env python3
"""A production rollback must be gated by a person, not by a model.

`policy_engine` Rule 4 used to hard-block a PROD rollback unless
`action.parameters["explicit_approval"]` was truthy. That single line had two
independent defects, and no test covered the rule at all.

1. **A human could not open the gate.** Nothing in the codebase writes
   `explicit_approval`. A `False` from `evaluate_action` is final in
   `policy_gate.decide`, and `_act_gate_node` builds the entire ACT report
   before it even loads the approval record — so the Slack approval that the
   whole system is built around never reaches the check. Live on 2026-09-14,
   incident f8ca9a54: a human approved the plan and the rollback still
   reported "Blocked by policy: ROLLBACK blocked on PROD: Requires explicit
   approval flag".

2. **A model could.** The only writer that could ever set the flag is the
   planner LLM, whose `parameters` are shaped by untrusted evidence —
   runbooks, pod logs, PR bodies. So the gate was satisfiable by prompt
   injection and unsatisfiable by an actual person.

The intent (no unattended rollback in production) now lives in
`policy_gate.decide` as a REQUIRES_APPROVAL floor, which a human *can* reach.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.policy_gate import (  # noqa: E402
    AutonomyDecision,
    decide,
)
from sre_agent.severity_engine import Severity, SeverityAssessment  # noqa: E402


@dataclass
class FakeAction:
    action_type: str
    target: str = "checkout-service"
    parameters: Dict[str, Any] = field(default_factory=dict)
    rollback_plan: Optional[str] = None


def sev(level: Severity) -> SeverityAssessment:
    return SeverityAssessment(
        severity=level,
        impact_score=0.5,
        urgency_score=0.5,
        impact_bucket="medium",
        urgency_bucket="medium",
    )


ALLOW = lambda a, e, r: (True, "allowed")  # noqa: E731
CALIBRATED = {
    "calibrated_action_probability": 0.99,
    "minimum_autonomy_probability": 0.95,
}


# --------------------------------------------------------------------------
# The policy engine itself
# --------------------------------------------------------------------------


def _evaluate(parameters=None, environment="production"):
    from sre_agent.policy_engine import evaluate_action

    return evaluate_action(
        FakeAction("rollback", parameters=parameters or {}), environment, 0.0
    )


def test_a_production_rollback_is_no_longer_hard_blocked():
    """A hard block here is final and no human can appeal it."""
    allowed, reason = _evaluate()
    assert allowed is True, reason


def test_the_untrusted_explicit_approval_flag_grants_nothing():
    """The planner writing this into its own parameters must not change the
    verdict in either direction — authorization is not readable from
    LLM-authored text."""
    with_flag, _ = _evaluate({"explicit_approval": True})
    without_flag, _ = _evaluate({})
    assert with_flag == without_flag is True


def test_the_old_blocking_reason_is_gone():
    _, reason = _evaluate()
    assert "explicit approval flag" not in reason.lower()


def test_a_non_dict_parameters_value_does_not_crash_the_gate():
    from sre_agent.policy_engine import evaluate_action

    action = FakeAction("rollback")
    action.parameters = "not a dict"  # type: ignore[assignment]
    allowed, _ = evaluate_action(action, "production", 0.0)
    assert allowed is True


# --------------------------------------------------------------------------
# The gate, which is where the intent now lives
# --------------------------------------------------------------------------


def test_a_production_rollback_is_held_for_a_human():
    """Even fully calibrated and low severity: nobody rolls production back
    unattended."""
    d = decide(
        FakeAction("rollback"),
        sev(Severity.SEV4),
        environment="production",
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert d.allowed_by_policy is True
    assert "production" in d.reason and "human approval" in d.reason


def test_a_rollback_outside_production_is_not_forced_to_a_human():
    """The floor is a production rule; staging must stay autonomous so the
    fix does not quietly become a global rollback freeze."""
    d = decide(
        FakeAction("rollback"),
        sev(Severity.SEV4),
        environment="staging",
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_the_floor_is_case_insensitive_about_the_environment_name():
    d = decide(
        FakeAction("rollback"),
        sev(Severity.SEV4),
        environment="PRODUCTION",
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_the_floor_does_not_catch_other_action_types():
    """A restart in production keeps whatever autonomy it earned."""
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        environment="production",
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_a_real_policy_block_still_wins_over_the_approval_floor():
    """Most-restrictive-wins: the floor must not upgrade a blocked action into
    something a human is invited to approve."""
    d = decide(
        FakeAction("rollback"),
        sev(Severity.SEV4),
        environment="production",
        evaluate_fn=lambda a, e, r: (False, "blocked by rule"),
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.BLOCKED
    assert d.allowed_by_policy is False


def test_the_held_decision_is_stable_across_the_approval_round_trip():
    """`_act_gate_node` validates the approval against a hash of the report it
    rebuilds after the human answers. If approving changed the decision, the
    hash would change and the approval would be rejected as a mismatch — so
    the pre- and post-approval verdicts have to be identical."""
    kwargs = dict(
        severity_assessment=sev(Severity.SEV4),
        environment="production",
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    before = decide(FakeAction("rollback"), **kwargs)
    after = decide(FakeAction("rollback"), **kwargs)
    assert before.decision is after.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert before.reason == after.reason


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
