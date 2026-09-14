#!/usr/bin/env python3
"""The planner's output has to survive the model's own encoding of it.

Live on 2026-09-14 every single planner invocation on this platform failed —
four for four, across three separate incidents — with:

    PlannerNode: Planning failed: 1 validation error for RemediationPlan
    actions
      Input should be a valid list [type=list_type,
      input_value='[{"action_type": "inspec... taken by this plan."}]',
      input_type=str]

The model had proposed real actions. It just handed the `actions` argument to
the function call as a JSON *string* instead of a list, Pydantic refused it,
and `_planner_node`'s except branch replaced the whole plan with one
hard-coded `escalate manual_review`. That is why `patch_resource_limits` had
never been exercised: no plan proposing it ever survived parsing.

The second half of the damage was what the human saw. The fallback was
rendered in Slack as "Proposed plan (1 action): escalate manual_review",
reasoned with whatever the policy gate happened to say — "unknown or
incomplete telemetry" — which is not why it escalated at all.
"""

from __future__ import annotations

import json

from sre_agent.agent_state import (
    ReflectorAnalysis,
    RemediationAction,
    RemediationPlan,
)
from sre_agent.approval_flow import format_approval_request

# The shape the model actually emitted, reduced to its essentials: the whole
# actions list arriving as one JSON string.
_ACTIONS_AS_A_STRING = json.dumps(
    [
        {
            "action_type": "inspect",
            "target": "thumb-worker",
            "parameters": {"container": "thumb-worker"},
            "safety_check": "Read-only; confirms the live memory limit.",
        },
        {
            "action_type": "config_change",
            "target": "thumb-worker",
            # The nested container arrives stringified too, by the same quirk.
            "parameters": json.dumps({"memory": "256Mi"}),
            "safety_check": "Raises the limit above the 150 MiB working set.",
            "rollback_plan": "kubectl set resources ... --limits=memory=64Mi",
        },
    ]
)


def _plan(**overrides):
    payload = {
        "plan_id": "plan-test",
        "hypothesis": "startup working set exceeds the memory limit",
        "actions": _ACTIONS_AS_A_STRING,
        "estimated_duration": "5 minutes",
        "risk_level": "medium",
        "verification_metrics": json.dumps(["container_memory_working_set_bytes"]),
    }
    payload.update(overrides)
    return RemediationPlan(**payload)


def test_a_stringified_actions_list_is_still_a_plan():
    plan = _plan()

    assert [a.action_type for a in plan.actions] == ["inspect", "config_change"]
    assert isinstance(plan.actions[0], RemediationAction)


def test_the_memory_limit_survives_a_stringified_parameters_object():
    """This is the field that decides whether anything can run.

    `live_tool_for_action` resolves `config_change` to `patch_resource_limits`
    only by finding memory/cpu inside `parameters`. A str there resolves to no
    tool, and the executor reports that as a capability gap — the platform
    blaming itself for a quoting artifact.
    """
    from sre_agent.executor import live_tool_for_action

    action = _plan().actions[1]

    assert action.parameters == {"memory": "256Mi"}
    assert live_tool_for_action(action) == "patch_resource_limits"


def test_a_stringified_list_of_strings_is_decoded_too():
    assert _plan().verification_metrics == ["container_memory_working_set_bytes"]


def test_a_real_list_is_left_exactly_alone():
    plan = _plan(actions=[{
        "action_type": "restart",
        "target": "thumb-worker",
        "safety_check": "single replica, already down",
    }])

    assert [a.action_type for a in plan.actions] == ["restart"]


def test_a_string_that_is_not_json_still_raises_the_real_error():
    """The coercion must not swallow genuinely bad output.

    If the model returns prose where a list belongs, that is a real failure
    and the planner's except branch is the right place for it to land.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _plan(actions="I could not determine any safe remediation.")


def test_the_reflectors_nested_evidence_is_decoded_the_same_way():
    analysis = ReflectorAnalysis(
        hypothesis="memory limit too low",
        confidence=0.8,
        reasoning="exit 137 on every start",
        causal_chain=json.dumps(
            [{"cause": "150 MiB warm-up", "effect": "OOMKill at 64Mi"}]
        ),
        unknowns=json.dumps(["whether the working set ever grows"]),
    )

    assert analysis.causal_chain[0].effect == "OOMKill at 64Mi"
    assert analysis.unknowns == ["whether the working set ever grows"]


def test_a_failed_planner_does_not_get_to_look_like_a_plan():
    """The honesty half.

    The reader has to learn that nothing was planned *before* they reach an
    action line whose stated reason came from the policy gate and names an
    unrelated cause.
    """
    from datetime import datetime, timezone

    text = format_approval_request(
        {
            "severity": "UNKNOWN",
            "aggregate_decision": "requires_approval",
            "confidence_status": "uncalibrated",
            "planning_failed": "1 validation error for RemediationPlan\nactions\n  Input should be a valid list",
            "action_reports": [
                {
                    "action_type": "escalate",
                    "target": "manual_review",
                    "namespace": "meridian",
                    "decision": "requires_approval",
                    "reversibility": "reversible",
                    "reason": "UNKNOWN: unknown or incomplete telemetry; human approval required",
                }
            ],
        },
        datetime(2026, 9, 14, 16, 35, 5, tzinfo=timezone.utc),
    )

    assert "planner failed" in text
    assert "not a recommendation" in text
    assert "1 validation error for RemediationPlan" in text
    # Before the action, not after it.
    assert text.index("planner failed") < text.index("escalate")


def test_a_real_plan_carries_no_such_warning():
    from datetime import datetime, timezone

    text = format_approval_request(
        {
            "severity": "HIGH",
            "aggregate_decision": "requires_approval",
            "action_reports": [
                {
                    "action_type": "config_change",
                    "target": "thumb-worker",
                    "decision": "requires_approval",
                    "reversibility": "reversible",
                    "reason": "memory limit change requires approval",
                }
            ],
        },
        datetime(2026, 9, 14, 16, 35, 5, tzinfo=timezone.utc),
    )

    assert "planner failed" not in text
