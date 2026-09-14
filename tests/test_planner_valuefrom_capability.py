#!/usr/bin/env python3
"""The planner must know what the execution boundary will refuse.

`patch_deployment_env` refuses to overwrite an env var the deployment sources
from a ConfigMap or Secret (`valueFrom`), because replacing an indirected
value with a literal silently detaches it from its owner. That refusal is
correct and stays. What was missing is that the planner was never told about
it, so it planned against an imagined capability surface.

Live on 2026-09-14, incident d3ca5138: the plan's single executable cluster
write was a `config_change` setting `CHAOS_MODE=false` on
deployment/checkout-service, whose description said the intent was to make
"the pod-spec value win over the ConfigMap-sourced chaos_mode key" — the
planner had the evidence that the key was `valueFrom` and proposed the one
action guaranteed to fail. The same plan's `escalate` even told a human to go
edit that ConfigMap by hand, so the model understood the boundary and still
spent the approval.

The approval message a human saw promised "1 change to the cluster". Sending
that exact call to the live edge returns:

    {"tool":"patch_deployment_env","status":"REFUSED","reason":"env var(s)
     ['CHAOS_MODE'] are sourced from a ConfigMap/Secret (valueFrom); the
     executor will not overwrite them with a literal"}

So approving it would have changed nothing, and the human would have been
told a cluster change was going to happen.
"""

from __future__ import annotations

import inspect

from sre_agent import graph_builder


def _planner_rules() -> str:
    return inspect.getsource(graph_builder._planner_node)


def test_the_planner_is_told_that_valuefrom_vars_cannot_be_written():
    rules = _planner_rules()
    assert "valueFrom" in rules
    assert "configMapKeyRef" in rules or "ConfigMap" in rules


def test_the_constraint_names_the_refusal_not_just_a_preference():
    """'Prefer not to' would leave the model free to try it anyway; the prompt
    has to say the action cannot run."""
    rules = _planner_rules()
    assert "CANNOT run" in rules or "cannot run" in rules


def test_the_planner_is_given_the_alternative_it_should_pick_instead():
    """A constraint with no escape hatch makes the model invent one. The
    d3ca5138 plan's own escalate was already the right answer."""
    rules = _planner_rules()
    valuefrom_clause = rules[rules.index("valueFrom"):]
    assert "escalate" in valuefrom_clause


def test_the_stopgap_rationalisation_is_named_explicitly():
    """The live failure was not the model missing the rule, it was the model
    talking itself past one: a 'stopgap' that would 'win over' the ConfigMap."""
    rules = _planner_rules()
    assert "stopgap" in rules
    assert "win over" in rules


def test_the_credential_refusal_is_still_stated():
    """The new clause sits beside the existing execution-boundary refusal and
    must not have displaced it."""
    rules = _planner_rules()
    assert "PASSWORD, TOKEN, SECRET, API_KEY" in rules
