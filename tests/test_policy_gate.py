#!/usr/bin/env python3
"""Unit tests for the Policy Gate (ACT phase).

Imported as a package module (``sre_agent.policy_gate``) because the gate uses a
relative import of the severity engine. A stub ``evaluate_fn`` is injected so the
tests never pull in the real ``policy_engine`` → ``agent_state`` → langchain
chain; the gate's own severity × reversibility logic is what we exercise here.
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
    classify_reversibility,
    decide,
    decide_plan,
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
        severity=level, impact_score=0.5, urgency_score=0.5,
        impact_bucket="medium", urgency_bucket="medium",
    )


ALLOW = lambda a, e, r: (True, "allowed")   # noqa: E731
BLOCK = lambda a, e, r: (False, "blocked by rule")  # noqa: E731


def test_reversible_low_severity_is_autonomous():
    d = decide(FakeAction("restart"), sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_reversible_high_severity_requires_approval():
    d = decide(FakeAction("restart"), sev(Severity.SEV1), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_risky_low_severity_with_rollback_is_autonomous():
    action = FakeAction("config_change", rollback_plan="kubectl apply previous configmap")
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_risky_low_severity_without_rollback_requires_approval():
    d = decide(FakeAction("config_change"), sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_scale_to_zero_is_irreversible_and_needs_approval_even_low_sev():
    action = FakeAction("scale", parameters={"replicas": 0})
    assert classify_reversibility(action) is Reversibility.IRREVERSIBLE
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_scale_up_is_risky_not_irreversible():
    action = FakeAction("scale", parameters={"replicas": 5}, rollback_plan="scale back to 2")
    assert classify_reversibility(action) is Reversibility.RISKY
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_hard_policy_block_wins():
    d = decide(FakeAction("restart"), sev(Severity.SEV4), evaluate_fn=BLOCK)
    assert d.decision is AutonomyDecision.BLOCKED
    assert d.allowed_by_policy is False


def test_plan_all_autonomous():
    actions = [FakeAction("restart"), FakeAction("rollback")]
    agg, per = decide_plan(actions, sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert agg is AutonomyDecision.AUTONOMOUS
    assert len(per) == 2


def test_plan_one_approval_downgrades_whole_plan():
    actions = [FakeAction("restart"), FakeAction("config_change")]  # 2nd has no rollback
    agg, _ = decide_plan(actions, sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert agg is AutonomyDecision.REQUIRES_APPROVAL


def test_plan_one_blocked_blocks_whole_plan():
    actions = [FakeAction("restart"), FakeAction("scale", parameters={"replicas": 0})]
    # Block only the scale-to-0 action.
    def selective(a, e, r):
        return (a.action_type != "scale", "policy")
    agg, _ = decide_plan(actions, sev(Severity.SEV4), evaluate_fn=selective)
    assert agg is AutonomyDecision.BLOCKED


def test_escalate_is_reversible_noop():
    assert classify_reversibility(FakeAction("escalate")) is Reversibility.REVERSIBLE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
