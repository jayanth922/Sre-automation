#!/usr/bin/env python3
"""The planner must be told the namespace its actions are scope-checked against.

`act_phase` blocks every action whose `parameters.namespace` differs from the
cluster's namespace. That check is correct — a scoped cluster must not reach
its neighbours — but the planner prompt never carried the namespace, so the
model inferred one from evidence: runbook snippets full of
`kubectl -n demo-app`, and MCP tool signatures whose default argument is
literally `namespace: str = "demo-app"` (services/edge_mcp_servers/.../executor_real).

Live on 2026-09-14, cluster namespace `meridian`, incident f8ca9a54: three of
five proposed actions — the rollback, the inspect and one escalate — were
blocked as "outside this cluster's scope 'meridian'". The plan aggregated to
`blocked`, leaving a human an escalate and a `code_fix` that maps to no tool.
"""

from __future__ import annotations

from sre_agent.graph_builder import planner_namespace_scope


def test_the_cluster_namespace_is_stated_in_the_prompt():
    clause = planner_namespace_scope("meridian")
    assert "meridian" in clause
    assert "parameters.namespace" in clause


def test_the_scope_is_marked_authoritative_not_retrieved_evidence():
    """Runbooks and past incidents are wrapped as untrusted; this is a fact
    from trusted state metadata and must not read as something to weigh."""
    clause = planner_namespace_scope("meridian")
    assert "authoritative" in clause.lower()
    assert "not evidence" in clause.lower()


def test_the_planner_is_told_not_to_copy_a_namespace_out_of_evidence():
    """The failure mode was copying `demo-app` from a runbook or a tool's
    default argument."""
    clause = planner_namespace_scope("meridian").lower()
    assert "runbook" in clause
    assert "example" in clause or "default" in clause


def test_a_genuinely_out_of_scope_fix_is_routed_to_a_human():
    """Being unable to act outside the namespace must produce an escalation,
    not a silently dropped action."""
    assert "escalate" in planner_namespace_scope("meridian")


def test_an_unscoped_cluster_gets_no_clause_at_all():
    """A cluster with no namespace restriction must not be handed an empty
    namespace to target."""
    assert planner_namespace_scope("") == ""
    assert planner_namespace_scope(None) == ""
    assert planner_namespace_scope("   ") == ""


def test_the_namespace_is_trimmed_before_it_reaches_the_prompt():
    clause = planner_namespace_scope("  meridian  ")
    assert '"meridian"' in clause
    assert "  meridian  " not in clause


def test_the_planner_prompt_actually_carries_the_scope():
    """Guards the wiring, not just the helper: the clause has to be
    interpolated into the prompt the planner sends."""
    import inspect

    from sre_agent import graph_builder

    source = inspect.getsource(graph_builder._planner_node)
    assert "planner_namespace_scope" in source
    assert "{namespace_scope}" in source
