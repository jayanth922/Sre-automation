#!/usr/bin/env python3
"""The planner must be able to remediate drift, not only escalate it.

Instruction 7 was added after incident d3ca5138 (2026-09-14), where the
planner proposed a `config_change` on a `valueFrom` env var that the
execution boundary was always going to refuse. That fix worked. It also
overshot: it left `escalate` as the *only* alternative for anything
ConfigMap-sourced, and `escalate` is the wrong answer when the ConfigMap is
already correct.

The Phase 1 pilot on 2026-09-19 (`checkout_high_latency`) is that overshoot
in production. Every stage did its job — the reflector named `CHAOS_MODE`
from configMap `meridian-config` as the cause, severity measured SEV3, and
PolicyGate cleared the plan to run unattended — and the plan was two
`inspect`s and a page. Nothing mutated, so nothing could be verified,
`outcome_class` fell to `dry_run`, and the run seeded no memory.

The escalation it did propose was also actionless. The ConfigMap read
`{"chaos_mode": "false", ...}` and the deployment env read `SLOW_RATE=0`,
while the live service reported `slow_rate=1.0`: the harness injects by HTTP
POST to `/admin/config`, so the fault lived only in the process's memory. The
declared state was already the state we wanted. Telling a human to go edit
`chaos_mode` would have asked them to set `false` to `false`.

So the missing move is `restart` — reconcile the running process to the
configuration it already has. These tests pin the instruction that says so,
and pin the boundary that keeps it from becoming "restart and see".
"""

from __future__ import annotations

import inspect

from sre_agent import graph_builder
from sre_agent.agent_state import RemediationAction
from sre_agent.executor import EXECUTOR_TOOL_MAP, build_rollback_command


def _planner_rules() -> str:
    return inspect.getsource(graph_builder._planner_node)


def _drift_clause() -> str:
    """The instruction-9 clause, from where it starts to the end of the rules.

    Collapsed to single-spaced text: the prompt is a wrapped triple-quoted
    string, so a phrase that reads as one line in the source can carry a
    newline and eight spaces in the middle of it.
    """
    rules = " ".join(_planner_rules().split())
    return rules[rules.index("DECLARED configuration"):]


def test_the_planner_is_told_to_restart_when_only_the_process_has_drifted():
    clause = _drift_clause()
    assert "restart" in clause
    assert "RUNNING" in clause


def test_the_advice_is_reachable_as_an_action_the_executor_can_run():
    """Prompt guidance that names an unroutable action is worse than silence:
    the plan looks like a remediation and executes as nothing."""
    assert "restart" in EXECUTOR_TOOL_MAP
    assert "recreate_pod" in EXECUTOR_TOOL_MAP
    assert RemediationAction(
        action_type="restart",
        target="checkout-service",
        safety_check="Restores the process to the declared SLOW_RATE=0.",
    ).action_type == "restart"


def test_restart_is_reversible_so_policy_can_clear_it_unattended():
    """PolicyGate's autonomous path needs a reversible action. A restart that
    reported no inverse would be held for approval and seed nothing, which is
    the same dead end by another route."""
    action = RemediationAction(
        action_type="restart",
        target="checkout-service",
        parameters={"namespace": "meridian"},
        safety_check="Reversible.",
    )
    assert build_rollback_command(action) == (
        "kubectl rollout undo deployment/checkout-service -n meridian"
    )


def test_the_valuefrom_escalation_is_preserved_not_replaced():
    """Instruction 7 is still correct for its own case. If restart displaced
    it, d3ca5138 comes straight back."""
    rules = _planner_rules()
    valuefrom_clause = rules[rules.index("valueFrom"):]
    assert "escalate" in valuefrom_clause
    assert "CANNOT run" in rules or "cannot run" in rules


def test_the_two_cases_are_distinguished_by_the_declared_value():
    """Without a stated discriminator the model has two rules pointing at the
    same evidence and picks by vibe. The declared value is what separates
    them: wrong declared value -> escalate, correct declared value -> restart."""
    clause = _drift_clause()
    assert "instruction 7" in clause
    assert "declared value is itself wrong" in clause


def test_restart_is_refused_as_a_remedy_for_an_undiagnosed_problem():
    """The failure mode this instruction could introduce is restart-as-guess.
    It has to be closed in the same breath that opens it."""
    clause = _drift_clause()
    assert "undiagnosed" in clause
    assert "guesswork" in clause
