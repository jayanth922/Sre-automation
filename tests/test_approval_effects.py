#!/usr/bin/env python3
"""The approval message must describe what approving actually does.

`format_approval_request` is the one message that gates the entire system: a
human reads it in Slack and types `approve fix`. It used to end with
"Approving runs N held actions against the cluster", counting straight off the
held list. Two action types make that sentence false, and both appeared in
live runs:

- incident 2c49ac9d (2026-09-14) held exactly two `escalate` actions — pure
  notifications — and was announced as two cluster writes.
- incident 8c925dbd held a `code_fix`, which is in no dispatch map and reports
  `SKIPPED: No MCP tool maps to action_type 'code_fix'` at run time. It was
  announced as something approving would run.

Overstating blast radius in the prompt that asks for consent is the failure
mode that matters here: it trains the on-call to discount the number.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sre_agent.approval_flow import format_approval_request
from sre_agent.executor import (
    EFFECT_CLUSTER_CHANGE,
    EFFECT_NO_CAPABILITY,
    EFFECT_NOTIFICATION,
    EFFECT_READ_ONLY,
    EFFECT_REPO_CHANGE,
    approval_effect,
    describe_approval_effects,
)


def _held(action_type: str, **parameters) -> dict:
    return {
        "action_type": action_type,
        "target": "payment-service",
        "namespace": "meridian",
        "parameters": parameters,
        "decision": "requires_approval",
        "reversibility": "reversible",
        "reason": "held for approval",
    }


# ---------------------------------------------------------------------------
# approval_effect — classify by capability, not by action name
# ---------------------------------------------------------------------------


def test_escalate_is_a_notification_not_a_cluster_write():
    """`escalate` is notify-only: it pages a human and mutates nothing."""
    assert approval_effect("escalate") == EFFECT_NOTIFICATION


def test_inspect_is_read_only():
    assert approval_effect("inspect") == EFFECT_READ_ONLY


def test_an_action_in_no_dispatch_map_is_reported_as_unexecutable():
    """`code_fix` reaches no tool map, so approving it runs nothing at all."""
    assert approval_effect("code_fix") == EFFECT_NO_CAPABILITY


def test_kubernetes_mutations_are_cluster_changes():
    for action_type in ("restart", "scale", "rollback", "recreate_pod"):
        assert approval_effect(action_type) == EFFECT_CLUSTER_CHANGE, action_type


def test_github_actions_change_the_repository_not_the_cluster():
    for action_type in ("revert_commit", "revert_pr", "comment_pr"):
        assert approval_effect(action_type) == EFFECT_REPO_CHANGE, action_type


def test_a_config_change_with_a_real_surface_is_a_cluster_change():
    """The only tool behind patch/config_change moves cpu/memory limits or env."""
    assert approval_effect("config_change", {"memory": "512Mi"}) == EFFECT_CLUSTER_CHANGE
    assert approval_effect("patch", {"cpu": "500m"}) == EFFECT_CLUSTER_CHANGE
    assert (
        approval_effect("config_change", {"env": {"PROVIDER_DOWN": "false"}})
        == EFFECT_CLUSTER_CHANGE
    )


def test_a_config_change_naming_no_supported_surface_cannot_run():
    """A ConfigMap or Helm value described in prose has no tool behind it."""
    assert approval_effect("config_change", {"note": "raise the pool size"}) == (
        EFFECT_NO_CAPABILITY
    )
    assert approval_effect("patch", None) == EFFECT_NO_CAPABILITY


def test_classification_is_case_and_whitespace_insensitive():
    assert approval_effect(" Escalate ") == EFFECT_NOTIFICATION
    assert approval_effect(None) == EFFECT_NO_CAPABILITY


# ---------------------------------------------------------------------------
# describe_approval_effects — the sentence itself
# ---------------------------------------------------------------------------


def test_two_escalations_are_never_described_as_cluster_writes():
    """The live 2c49ac9d plan: two held escalations, zero infrastructure change."""
    sentence = describe_approval_effects([_held("escalate"), _held("escalate")])
    assert sentence == (
        "Approving runs 2 held actions: 2 notifications to humans "
        "(no system change)."
    )
    assert "against the cluster" not in sentence


def test_a_held_code_fix_is_announced_as_unexecutable():
    sentence = describe_approval_effects([_held("code_fix")])
    assert sentence == (
        "Approving runs 1 held action: 1 action Sentinel cannot execute "
        "(it will be skipped)."
    )


def test_a_mixed_plan_is_broken_down_riskiest_first():
    sentence = describe_approval_effects(
        [_held("restart"), _held("code_fix"), _held("escalate")]
    )
    assert sentence == (
        "Approving runs 3 held actions: 1 change to the cluster, "
        "1 notification to a human (no system change), "
        "1 action Sentinel cannot execute (it will be skipped)."
    )
    assert sentence.index("change to the cluster") < sentence.index("notification")


def test_plurals_read_correctly_for_every_effect():
    """An earlier draft pluralized the first word — "read-onlys check"."""
    sentence = describe_approval_effects(
        [_held("inspect"), _held("inspect"), _held("revert_pr"), _held("revert_pr")]
    )
    assert "2 changes to the repository" in sentence
    assert "2 read-only checks (no system change)" in sentence
    assert "read-onlys" not in sentence


def test_only_held_actions_are_counted():
    """Autonomous actions already ran; blocked ones never will. Neither is
    something the approver is being asked to authorize."""
    reports = [
        _held("restart"),
        {**_held("inspect"), "decision": "autonomous"},
        {**_held("config_change"), "decision": "blocked"},
    ]
    assert describe_approval_effects(reports) == (
        "Approving runs 1 held action: 1 change to the cluster."
    )


def test_nothing_held_renders_no_sentence_at_all():
    assert describe_approval_effects([]) == ""
    assert describe_approval_effects(None) == ""
    assert describe_approval_effects([{**_held("restart"), "decision": "blocked"}]) == ""


def test_malformed_reports_do_not_break_the_gate_message():
    """A missing `parameters` key must not stop the approval prompt rendering."""
    sentence = describe_approval_effects(
        ["not-a-dict", {"action_type": "escalate", "decision": "requires_approval"}]
    )
    assert sentence == (
        "Approving runs 1 held action: 1 notification to a human "
        "(no system change)."
    )


# ---------------------------------------------------------------------------
# end to end through the Slack message a human actually reads
# ---------------------------------------------------------------------------


def _expires() -> datetime:
    return datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def test_the_slack_approval_prompt_for_2c49ac9d_claims_no_cluster_write():
    """Reproduces the live inventory-service plan: 2 autonomous inspects and
    2 held escalations. Nothing about it touches the cluster."""
    payload = {
        "severity": "UNKNOWN",
        "aggregate_decision": "requires_approval",
        "confidence_status": "uncalibrated",
        "action_reports": [
            {**_held("inspect"), "decision": "autonomous"},
            {**_held("inspect"), "decision": "autonomous"},
            _held("escalate"),
            _held("escalate"),
        ],
    }
    text = format_approval_request(payload, _expires())
    assert "against the cluster" not in text
    assert "2 notifications to humans (no system change)" in text
    assert "approve fix" in text


def test_the_prompt_still_names_the_cluster_when_a_cluster_write_is_held():
    payload = {
        "severity": "UNKNOWN",
        "aggregate_decision": "requires_approval",
        "action_reports": [_held("restart"), _held("escalate")],
    }
    text = format_approval_request(payload, _expires())
    assert "1 change to the cluster" in text
    assert "1 notification to a human (no system change)" in text


def test_a_plan_with_nothing_held_does_not_print_an_effects_line():
    payload = {
        "severity": "UNKNOWN",
        "aggregate_decision": "blocked",
        "action_reports": [{**_held("config_change"), "decision": "blocked"}],
    }
    text = format_approval_request(payload, _expires())
    assert "Approving runs" not in text
    assert "approve fix" in text
