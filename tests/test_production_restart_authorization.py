#!/usr/bin/env python3
"""A production restart must be gated by measured state, not by the planner's
own label — and never by a verdict no human can appeal.

`policy_engine` Rule 1 used to hard-block a PROD restart whenever `risk_score`
was at or above `POLICY_RESTART_RISK_THRESHOLD` (default 3.0). It is the same
shape as the Rule 4 rollback defect that `test_production_rollback_authorization.py`
covers, and it had the same two independent halves.

1. **A human could not open the gate.** A `False` from `evaluate_action` is
   final in `policy_gate.decide`, and `_act_gate_node` builds the entire ACT
   report before it loads the approval record. Live on 2026-09-23, the
   inventory_slow_queries trial: the agent identified the slow query, proposed
   the correct restart, and closed UNRESOLVED anyway — "Blocked by policy:
   RESTART blocked on PROD: Risk score 5.0 >= 3.0" parked the incident at
   `awaiting_approval`, which nothing clears.

2. **A model could.** `risk_score` is `act_phase._plan_risk_score`, i.e. the
   plan's own `risk_level` string mapped low=2.0 / medium=5.0 / high=8.0 with
   5.0 as the default. Against a 3.0 threshold, "low" was the only value that
   passed — so a planner reading untrusted evidence could authorize its own
   production restart by labelling its plan, while every plan that declined to
   self-assess was blocked forever.

The intent (no reckless restart of production) now lives in `policy_gate`,
which holds a restart for a human on unknown telemetry, on a severity above
the autonomy band, and on an uncalibrated or below-threshold remediation
probability — all of them measured, and all of them clearable by a person.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.policy_gate import (  # noqa: E402
    AutonomyDecision,
    Reversibility,
    decide,
    decide_plan,
)
from sre_agent.severity_engine import Severity, SeverityAssessment  # noqa: E402


@dataclass
class FakeAction:
    action_type: str
    target: str = "inventory-service"
    parameters: Dict[str, Any] = field(default_factory=dict)
    rollback_plan: Optional[str] = None


def sev(level: Severity, unknown: bool = False) -> SeverityAssessment:
    return SeverityAssessment(
        severity=level,
        impact_score=0.5,
        urgency_score=0.5,
        impact_bucket="medium",
        urgency_bucket="medium",
        unknown_telemetry=unknown,
    )


ALLOW = lambda a, e, r: (True, "allowed")  # noqa: E731
CALIBRATED = {
    "calibrated_action_probability": 0.99,
    "minimum_autonomy_probability": 0.95,
}
# What trial 5 actually carried: a model self-report, no calibration artifact.
UNCALIBRATED: Dict[str, Any] = {
    "calibrated_action_probability": None,
    "minimum_autonomy_probability": None,
}
# `medium` risk_level, the planner default, is what produced the live block.
TRIAL_5_RISK = 5.0


# --------------------------------------------------------------------------
# The policy engine itself
# --------------------------------------------------------------------------


def _evaluate(risk_score=TRIAL_5_RISK, environment="production", action_type="restart"):
    from sre_agent.policy_engine import evaluate_action

    return evaluate_action(FakeAction(action_type), environment, risk_score)


def test_a_production_restart_is_no_longer_hard_blocked():
    """A hard block here is final; no human can appeal it."""
    allowed, reason = _evaluate()
    assert allowed is True, reason


def test_the_planner_cannot_decide_its_own_production_restart_by_labelling_it():
    """`risk_score` is the plan's self-reported `risk_level`. It must not move
    the verdict in either direction — authorization is not readable from
    LLM-authored text."""
    verdicts = {_evaluate(risk_score=score)[0] for score in (0.0, 2.0, 5.0, 8.0, 10.0)}
    assert verdicts == {True}


def test_the_threshold_environment_knob_no_longer_decides_anything():
    """The knob is gone. Set to a value that blocked every possible score
    before, the restart is still allowed through to the gate."""
    import os

    os.environ["POLICY_RESTART_RISK_THRESHOLD"] = "0.0"
    try:
        allowed, _ = _evaluate(risk_score=0.0)
    finally:
        os.environ.pop("POLICY_RESTART_RISK_THRESHOLD", None)
    assert allowed is True


def test_the_old_blocking_reason_is_gone():
    _, reason = _evaluate()
    assert "risk score" not in reason.lower()
    assert "blocked" not in reason.lower()


def test_the_other_production_blocks_still_hold():
    """The rule was removed, not the file. Scale-to-0 is still refused, and it
    is refused deterministically rather than on a model's label."""
    from sre_agent.policy_engine import evaluate_action

    allowed, reason = evaluate_action(
        FakeAction("scale", parameters={"replicas": 0}), "production", 0.0
    )
    assert allowed is False
    assert "outage" in reason.lower()


# --------------------------------------------------------------------------
# The gate, which is where the intent now lives
# --------------------------------------------------------------------------


def test_an_uncalibrated_production_restart_is_held_for_a_human_not_blocked():
    """Trial 5's exact shape, through the real policy engine (evaluate_fn
    default). The distinction is the whole fix: BLOCKED is terminal and
    unappealable, REQUIRES_APPROVAL is a question a person can answer."""
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV1),
        environment="production",
        risk_score=TRIAL_5_RISK,
        **UNCALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert d.allowed_by_policy is True
    assert "uncalibrated" in d.reason.lower()


def test_a_high_severity_production_restart_is_held_for_a_human():
    """Severity comes from telemetry, not from the plan's own label."""
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV1),
        environment="production",
        risk_score=TRIAL_5_RISK,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert d.allowed_by_policy is True


def test_unknown_telemetry_holds_a_production_restart():
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4, unknown=True),
        environment="production",
        risk_score=0.0,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_a_calibrated_low_severity_production_restart_is_autonomous():
    """The path Rule 1 made unreachable. A restart is REVERSIBLE; once
    telemetry is known, severity is inside the autonomy band and a measured
    calibration artifact clears the threshold, it may run unattended. Without
    this, ACT can never resolve anything on its own."""
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        environment="production",
        risk_score=TRIAL_5_RISK,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS
    assert d.reversibility is Reversibility.REVERSIBLE


def test_a_restart_outside_production_is_unaffected():
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        environment="staging",
        risk_score=10.0,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_a_real_policy_block_still_wins():
    """Most-restrictive-wins is intact: removing Rule 1 must not turn a
    genuine policy refusal into something a human is invited to approve."""
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        environment="production",
        risk_score=0.0,
        evaluate_fn=lambda a, e, r: (False, "blocked by rule"),
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.BLOCKED
    assert d.allowed_by_policy is False


def test_the_held_decision_is_stable_across_the_approval_round_trip():
    """`_act_gate_node` validates the approval against a hash of the report it
    rebuilds after the human answers. If approving changed the decision the
    hash would change and the approval would be rejected as a mismatch."""
    kwargs = dict(
        severity_assessment=sev(Severity.SEV1),
        environment="production",
        risk_score=TRIAL_5_RISK,
        **UNCALIBRATED,
    )
    before = decide(FakeAction("restart"), **kwargs)
    after = decide(FakeAction("restart"), **kwargs)
    assert before.decision is after.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert before.reason == after.reason


# --------------------------------------------------------------------------
# The plan, where a per-action rule met a plan-level score
# --------------------------------------------------------------------------


def test_one_restart_no_longer_blocks_the_whole_plan():
    """`decide_plan` hands every action the same plan-level `risk_score`, and
    a single BLOCKED action blocks the plan. Trial 5's plan — inspect, restart,
    escalate — was therefore dead as a whole, with the escalation that existed
    only to report the block dragged down with it."""
    aggregate, per_action = decide_plan(
        [FakeAction("inspect"), FakeAction("restart"), FakeAction("escalate")],
        sev(Severity.SEV1),
        "production",
        TRIAL_5_RISK,
        None,
        **UNCALIBRATED,
    )
    assert aggregate is AutonomyDecision.REQUIRES_APPROVAL
    assert not any(d.decision is AutonomyDecision.BLOCKED for d in per_action)
    assert per_action[0].decision is AutonomyDecision.AUTONOMOUS  # read-only
    assert per_action[1].decision is AutonomyDecision.REQUIRES_APPROVAL


def test_proposing_a_restart_no_longer_raises_the_score_that_judged_it():
    """`calculate_risk_score` adds 0.5 per "dangerous" action, restart among
    them, so a plan containing a restart inflated the very number Rule 1 read
    back. Nothing about a plan's composition may decide a production restart
    any more."""
    from sre_agent.policy_engine import calculate_risk_score

    class Plan:
        def __init__(self, actions, risk_level="medium"):
            self.actions = actions
            self.risk_level = risk_level

    one = calculate_risk_score(Plan([FakeAction("restart")]))
    many = calculate_risk_score(
        Plan([FakeAction("restart")] * 4 + [FakeAction("rollback")] * 2)
    )
    assert many > one  # the score still moves; it simply no longer gates

    verdicts = {_evaluate(risk_score=score)[0] for score in (one, many)}
    assert verdicts == {True}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
