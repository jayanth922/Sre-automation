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
CALIBRATED = {
    "calibrated_action_probability": 0.99,
    "minimum_autonomy_probability": 0.95,
}


def test_reversible_low_severity_is_autonomous():
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_reversible_high_severity_requires_approval():
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV1),
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_risky_low_severity_with_rollback_is_autonomous():
    action = FakeAction("config_change", rollback_plan="kubectl apply previous configmap")
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_risky_low_severity_without_rollback_requires_approval():
    d = decide(
        FakeAction("config_change"),
        sev(Severity.SEV4),
        evaluate_fn=ALLOW,
        **CALIBRATED,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_scale_to_zero_is_irreversible_and_needs_approval_even_low_sev():
    action = FakeAction("scale", parameters={"replicas": 0})
    assert classify_reversibility(action) is Reversibility.IRREVERSIBLE
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_scale_up_is_risky_not_irreversible():
    action = FakeAction("scale", parameters={"replicas": 5}, rollback_plan="scale back to 2")
    assert classify_reversibility(action) is Reversibility.RISKY
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_recreate_pod_is_reversible_like_restart():
    action = FakeAction("recreate_pod", target="checkout-service-7d9f-x2k4p")
    assert classify_reversibility(action) is Reversibility.REVERSIBLE
    d = decide(action, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_recreate_pod_high_severity_requires_approval():
    action = FakeAction("recreate_pod", target="checkout-service-7d9f-x2k4p")
    d = decide(action, sev(Severity.SEV1), evaluate_fn=ALLOW, **CALIBRATED)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL


def test_hard_policy_block_wins():
    d = decide(FakeAction("restart"), sev(Severity.SEV4), evaluate_fn=BLOCK)
    assert d.decision is AutonomyDecision.BLOCKED
    assert d.allowed_by_policy is False


def test_plan_all_autonomous():
    # `recreate_pod` replaces the `rollback` this used to pair with `restart`:
    # `decide_plan` defaults to environment="production", where a rollback is
    # now always held for a human. Both of these are REVERSIBLE and stay
    # autonomous, so the aggregation logic under test is unchanged.
    actions = [FakeAction("restart"), FakeAction("recreate_pod")]
    agg, per = decide_plan(
        actions, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED
    )
    assert agg is AutonomyDecision.AUTONOMOUS
    assert len(per) == 2


def test_plan_with_a_production_rollback_is_downgraded_to_approval():
    """The pairing the test above gave up, asserted directly."""
    actions = [FakeAction("restart"), FakeAction("rollback")]
    agg, per = decide_plan(
        actions, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED
    )
    assert agg is AutonomyDecision.REQUIRES_APPROVAL
    assert per[0].decision is AutonomyDecision.AUTONOMOUS
    assert per[1].decision is AutonomyDecision.REQUIRES_APPROVAL


def test_plan_one_approval_downgrades_whole_plan():
    actions = [FakeAction("restart"), FakeAction("config_change")]  # 2nd has no rollback
    agg, _ = decide_plan(
        actions, sev(Severity.SEV4), evaluate_fn=ALLOW, **CALIBRATED
    )
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


def test_uncalibrated_self_confidence_cannot_authorize_mutation():
    d = decide(FakeAction("restart"), sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert d.confidence_calibrated is False
    assert "uncalibrated" in d.reason


def test_calibrated_probability_below_measured_threshold_requires_approval():
    d = decide(
        FakeAction("restart"),
        sev(Severity.SEV4),
        evaluate_fn=ALLOW,
        calibrated_action_probability=0.89,
        minimum_autonomy_probability=0.95,
    )
    assert d.decision is AutonomyDecision.REQUIRES_APPROVAL
    assert d.confidence_calibrated is True


def test_notify_only_escalation_does_not_require_calibration():
    d = decide(FakeAction("escalate"), sev(Severity.SEV4), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


# --- Read-only actions ---------------------------------------------------
# Inspection writes nothing, so no severity, telemetry gap or calibration
# argument can make it unsafe. Gating it was how diagnostics ended up disguised
# as `config_change`, burning a human approval on a step that changes nothing.


def test_inspect_is_read_only():
    assert classify_reversibility(FakeAction("inspect")) is Reversibility.READ_ONLY


@pytest.mark.parametrize("level", [Severity.SEV1, Severity.SEV2, Severity.SEV4])
def test_inspect_is_autonomous_at_every_severity(level):
    d = decide(FakeAction("inspect"), sev(level), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS
    assert "mutates nothing" in d.reason


def test_inspect_is_autonomous_with_unknown_telemetry():
    d = decide(FakeAction("inspect"), sev(Severity.UNKNOWN), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_inspect_does_not_require_calibration():
    d = decide(
        FakeAction("inspect"),
        sev(Severity.SEV4),
        evaluate_fn=ALLOW,
        calibrated_action_probability=0.10,
        minimum_autonomy_probability=0.95,
    )
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_a_hard_policy_block_still_wins_over_read_only():
    # Reading another tenant's namespace is still a policy matter.
    d = decide(FakeAction("inspect"), sev(Severity.SEV4), evaluate_fn=BLOCK)
    assert d.decision is AutonomyDecision.BLOCKED


def test_read_only_action_types_match_the_executor():
    # The gate keeps its own literal set to stay off the executor's import
    # chain; if the two ever drift, a read-only action added in one place would
    # be gated (or ungated) in the other.
    from sre_agent.executor import READ_ONLY_ACTIONS
    from sre_agent.policy_gate import _READ_ONLY_ACTION_TYPES

    assert set(_READ_ONLY_ACTION_TYPES) == set(READ_ONLY_ACTIONS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------- #
# Remedy fit — safe is not the same as useful
# --------------------------------------------------------------------------- #

from sre_agent.policy_gate import (  # noqa: E402
    diagnosed_memory_leak,
    unfit_remedy_reason,
)


@pytest.mark.parametrize(
    "text",
    [
        "checkout-service has a memory leak in the request path",
        "Memory-leak: the cache leaks memory on every request",
        "unbounded heap growth until OOMKilled",
    ],
)
def test_leak_diagnoses_are_recognised(text):
    assert diagnosed_memory_leak(None, text)


@pytest.mark.parametrize(
    "text",
    [
        "connection pool leak exhausts database connections",
        "load spike drove memory usage up; working set is stable per request",
        None,
    ],
)
def test_other_diagnoses_are_not_a_memory_leak(text):
    assert not diagnosed_memory_leak(text)


def test_scale_and_a_memory_limit_raise_cannot_fix_a_leak():
    assert "memory leak" in unfit_remedy_reason(
        FakeAction("scale", parameters={"replicas": 4}), True
    )
    assert "postpones" in unfit_remedy_reason(
        FakeAction("config_change", parameters={"memory": "1Gi"}), True
    )


def test_restart_and_env_changes_remain_fit_for_a_leak():
    assert unfit_remedy_reason(FakeAction("restart"), True) is None
    assert (
        unfit_remedy_reason(
            FakeAction("config_change", parameters={"env": {"CACHE_ENABLED": "false"}}),
            True,
        )
        is None
    )


def test_scale_is_fit_when_no_leak_was_diagnosed():
    assert unfit_remedy_reason(FakeAction("scale", parameters={"replicas": 4}), False) is None


# --------------------------------------------------------------------------- #
# External provider outage — nothing in this service fixes it
# --------------------------------------------------------------------------- #

from sre_agent.policy_gate import diagnosed_dependency_outage  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        # Phrasings from the E2E Run 2 (2026-09-29) investigation.
        "This is a genuine external dependency outage, not a Meridian regression.",
        "The external payment provider went down at 22:14:20Z.",
        "Confirmed — this is a real provider outage, not noise.",
        "the payment provider is genuinely down",
        "payment_provider_up flipped to 0 at 22:14:20Z",
        "every charge fails with provider_down",
        "a third-party API is unavailable",
    ],
)
def test_provider_outage_diagnoses_are_recognised(text):
    assert diagnosed_dependency_outage(None, text)


@pytest.mark.parametrize(
    "text",
    [
        "This is not a provider outage; checkout regressed in revision 13.",
        "We ruled out an external dependency failure.",
        "the provider is not down",
        "Rules out provider_down: payment_provider_up stayed at 1.",
        # Internal dependencies are fixed inside the cluster.
        "checkout fails because upstream inventory-service is crash-looping",
        "downstream dependency failure: payment-service crashed",
        "A memory leak in checkout grows the heap to the limit.",
    ],
)
def test_other_diagnoses_are_not_a_provider_outage(text):
    assert not diagnosed_dependency_outage(text)


@pytest.mark.parametrize("action_type", ["restart", "rollback", "revert_commit", "scale"])
def test_changing_this_service_cannot_fix_a_provider_outage(action_type):
    reason = unfit_remedy_reason(
        FakeAction(action_type, "payment-service"), False, dependency_outage=True
    )
    assert reason and "external provider outage" in reason


@pytest.mark.parametrize("action_type", ["escalate", "inspect", "config_change"])
def test_escalation_and_failover_stay_fit_for_a_provider_outage(action_type):
    # config_change stays open: failing over to a secondary provider is a real
    # response to a provider outage.
    assert (
        unfit_remedy_reason(
            FakeAction(action_type, "payment-service", {"env": {"PROVIDER": "backup"}}),
            False,
            dependency_outage=True,
        )
        is None
    )


def test_restart_is_fit_when_no_outage_was_diagnosed():
    assert unfit_remedy_reason(FakeAction("restart"), False) is None


# --------------------------------------------------------------------------- #
# Escalation is never held — it is how the human finds out
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", [Severity.SEV1, Severity.SEV2, Severity.SEV4])
def test_escalate_is_autonomous_at_every_severity(level):
    # E2E Run 2 (2026-09-29): the SEV1 page waited on the restart's approval.
    d = decide(FakeAction("escalate"), sev(level), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS
    assert "notify-only" in d.reason


def test_escalate_is_autonomous_with_unknown_telemetry():
    d = decide(FakeAction("escalate"), sev(Severity.UNKNOWN), evaluate_fn=ALLOW)
    assert d.decision is AutonomyDecision.AUTONOMOUS


def test_a_hard_policy_block_still_wins_over_escalation():
    d = decide(FakeAction("escalate"), sev(Severity.SEV1), evaluate_fn=BLOCK)
    assert d.decision is AutonomyDecision.BLOCKED
