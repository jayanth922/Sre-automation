#!/usr/bin/env python3
"""The Kubernetes specialist that investigates without appearing in the chat.

The supervisor prompt has always described a team of five with four visible in
the timeline, "Kubernetes stays internal only". Only the invisibility was ever
implemented: the node was dropped from the graph, so no incident investigation
ever read the cluster's own declared state. Live slow-query incidents escalated
to a human twice while the cause sat in the deployment's env the whole time.

These tests pin the three halves of the fix: the prescan runs (and knows when
not to), what it reads is redacted at the source before it reaches an LLM, and
it stays out of the visible cast without being erased from the record.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent import graph_builder  # noqa: E402
from sre_agent.incident_timeline import (  # noqa: E402
    build_supervisor_summary_content,
)

_SPEC_VIEW_PATH = (
    Path(__file__).resolve().parents[1]
    / "edge_mcp_servers" / "mcp_servers" / "k8s_real" / "spec_view.py"
)
_spec = importlib.util.spec_from_file_location("k8s_spec_view", _SPEC_VIEW_PATH)
spec_view = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = spec_view
_spec.loader.exec_module(spec_view)


# --------------------------------------------------------------------------
# The prescan node
# --------------------------------------------------------------------------


def _env(name, value=None, value_from=None):
    return SimpleNamespace(name=name, value=value, value_from=value_from)


def test_the_prescan_reads_the_cluster_before_anything_else_runs():
    calls = []

    async def kubernetes_agent(state):
        calls.append(state)
        return {"agent_results": {"kubernetes_agent": "FAULT_INJECTION_ENABLED=true"}}

    node = graph_builder._make_infra_prescan_node(kubernetes_agent)
    out = asyncio.run(node({"metadata": {}, "agents_invoked": [], "agent_results": {}}))

    assert len(calls) == 1
    assert out["agent_results"]["kubernetes_agent"] == "FAULT_INJECTION_ENABLED=true"


def test_a_follow_up_question_does_not_re_read_the_cluster():
    """Assistant mode re-enters the graph for every thread reply; the cluster
    was already read for this incident."""

    async def kubernetes_agent(state):  # pragma: no cover - must never run
        raise AssertionError("a follow-up must not spend a specialist turn")

    node = graph_builder._make_infra_prescan_node(kubernetes_agent)
    out = asyncio.run(
        node({"metadata": {"conversation_mode": "assistant"}, "agents_invoked": []})
    )
    assert out == {}


def test_the_prescan_runs_once_per_investigation():
    async def kubernetes_agent(state):  # pragma: no cover - must never run
        raise AssertionError("already invoked for this incident")

    node = graph_builder._make_infra_prescan_node(kubernetes_agent)
    out = asyncio.run(
        node({"metadata": {}, "agents_invoked": ["kubernetes_agent"], "agent_results": {}})
    )
    assert out == {}


def test_a_broken_prescan_does_not_abort_the_investigation():
    async def kubernetes_agent(state):
        raise RuntimeError("k8s MCP server unreachable")

    node = graph_builder._make_infra_prescan_node(kubernetes_agent)
    out = asyncio.run(
        node({"metadata": {}, "agents_invoked": [], "agent_results": {"x": "y"}})
    )
    # The failure is recorded as evidence, not raised: the visible specialists
    # still have their own.
    assert "unreachable" in out["agent_results"]["kubernetes_agent"]
    assert out["agent_results"]["x"] == "y"


def test_the_graph_runs_the_prescan_between_prepare_and_the_supervisor():
    source = (
        Path(__file__).resolve().parents[1] / "sre_agent" / "graph_builder.py"
    ).read_text()
    assert 'workflow.add_node("infra_prescan"' in source
    assert 'workflow.add_edge("prepare", "infra_prescan")' in source
    assert 'workflow.add_edge("infra_prescan", "supervisor")' in source
    # ...and never as a supervisor routing target: it is not a chat participant.
    assert '"kubernetes_agent": "kubernetes_agent"' not in source


# --------------------------------------------------------------------------
# What the read is allowed to say out loud
# --------------------------------------------------------------------------


def test_plain_configuration_is_reported_with_its_value():
    """The whole point: a feature flag the agent can see is a cause it can fix."""
    container = SimpleNamespace(
        name="inventory-service",
        image="inventory:1.4.2",
        image_pull_policy="IfNotPresent",
        env=[_env("FAULT_INJECTION_ENABLED", "true"), _env("SLOW_QUERY_RATE", "0.6")],
        resources=SimpleNamespace(limits={"memory": "256Mi"}, requests={"cpu": "100m"}),
        env_from=None,
    )
    out = spec_view.format_container_spec(container)

    assert out["image"] == "inventory:1.4.2"
    assert out["env"] == {"FAULT_INJECTION_ENABLED": "true", "SLOW_QUERY_RATE": "0.6"}
    assert out["limits"] == {"memory": "256Mi"}
    assert "env_redacted" not in out


def test_inline_credentials_never_leave_the_cluster():
    """A deployment read becomes LLM prompt text and then a Slack message, so
    a value someone pasted into the pod spec is stripped here, at the source."""
    container = SimpleNamespace(
        name="api",
        image="api:1",
        image_pull_policy=None,
        env=[
            _env("DATABASE_PASSWORD", "hunter2"),
            _env("STRIPE_API_KEY", "sk_live_abc"),
            _env("SESSION_KEY", "s3cr3t"),
            _env("JWT_SIGNING_SECRET", "nope"),
            _env("LOG_LEVEL", "debug"),
        ],
        resources=None,
        env_from=None,
    )
    out = spec_view.format_container_spec(container)

    assert out["env"]["LOG_LEVEL"] == "debug"
    for name in ("DATABASE_PASSWORD", "STRIPE_API_KEY", "SESSION_KEY", "JWT_SIGNING_SECRET"):
        assert out["env"][name] == spec_view.REDACTED, name
    assert set(out["env_redacted"]) == {
        "DATABASE_PASSWORD",
        "STRIPE_API_KEY",
        "SESSION_KEY",
        "JWT_SIGNING_SECRET",
    }
    assert "hunter2" not in str(out)
    assert "sk_live_abc" not in str(out)


def test_indirect_values_are_named_by_source_never_resolved():
    container = SimpleNamespace(
        name="api",
        image="api:1",
        image_pull_policy=None,
        env=[
            _env(
                "DB_URL",
                None,
                SimpleNamespace(
                    secret_key_ref=SimpleNamespace(name="db-creds", key="url"),
                    config_map_key_ref=None,
                    field_ref=None,
                    resource_field_ref=None,
                ),
            ),
            _env(
                "REGION",
                None,
                SimpleNamespace(
                    secret_key_ref=None,
                    config_map_key_ref=SimpleNamespace(name="app-config", key="region"),
                    field_ref=None,
                    resource_field_ref=None,
                ),
            ),
        ],
        resources=None,
        env_from=[
            SimpleNamespace(config_map_ref=SimpleNamespace(name="shared"), secret_ref=None)
        ],
    )
    out = spec_view.format_container_spec(container)

    # "set, but indirected" must stay distinguishable from "not set".
    assert out["env"] == {"DB_URL": None, "REGION": None}
    assert out["env_sources"] == {
        "DB_URL": "secret:db-creds/url",
        "REGION": "configMap:app-config/region",
    }
    # envFrom keys appear nowhere in `env`; say so rather than let the agent
    # conclude the container is unconfigured.
    assert out["env_from"] == ["configMap:shared"]


def test_the_new_read_is_namespace_scoped_like_every_other_cluster_read():
    """A tool absent from _NAMESPACE_ARG_TOOLS is simply not enforced, so a
    scoped tenant could read another namespace's env."""
    from sre_agent.namespace_scope import _NAMESPACE_ARG_TOOLS

    assert "get_deployment_spec" in _NAMESPACE_ARG_TOOLS


def test_the_kubernetes_specialist_is_allowed_the_tool_it_needs():
    import yaml

    config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "sre_agent" / "config" / "agent_config.yaml"
        ).read_text()
    )
    assert "get_deployment_spec" in config["agents"]["kubernetes_agent"]["tools"]


# --------------------------------------------------------------------------
# Internal, but not erased
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_plan_pointer_counts_only_the_specialists_the_plan_is_about(monkeypatch):
    """The plan is written for the visible queue, but `agents_invoked` now also
    carries the prescan — counting it would announce step 3's work while
    handing off step 2."""
    import sre_agent.supervisor as supervisor_module
    from sre_agent.supervisor import SupervisorAgent

    async def fake_emit(*args, **kwargs):
        return SimpleNamespace(id="evt")

    async def fake_narrate(*args, **kwargs):
        return ""

    monkeypatch.setattr(supervisor_module, "emit_timeline_event", fake_emit)
    monkeypatch.setattr(supervisor_module, "narrate_supervisor_handoff", fake_narrate)

    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.formatter = None
    supervisor.llm = None
    supervisor.system_prompt = ""

    result = await supervisor.route(
        {
            "current_query": "Investigate alert: InventorySlowQueries",
            "metadata": {
                "investigation_plan": {
                    "steps": ["Check latency metrics", "Check error logs"],
                    "agents_sequence": ["metrics_agent", "logs_agent"],
                    "complexity": "complex",
                    "auto_execute": False,
                    "reasoning": "Latency alert with no obvious deploy correlation.",
                },
                "specialist_queue": ["metrics_agent", "logs_agent"],
            },
            # The prescan ran first, then the first visible specialist.
            "agents_invoked": ["kubernetes_agent", "metrics_agent"],
            "agent_results": {},
            "thought_traces": {},
            "incident_id": "11111111-1111-1111-1111-111111111111",
        }
    )

    assert result["next"] == "logs_agent"
    assert result["metadata"]["plan_step"] == 1
    assert result["metadata"]["routing_reasoning"] == (
        "Executing plan step 2: Check error logs"
    )


def test_the_summary_credits_the_visible_cast_and_records_the_internal_source():
    _content, payload = build_supervisor_summary_content(
        "Root cause: fault injection left enabled.",
        {
            "metrics_agent": "p90 jumped to 2.2s at 19:07",
            "kubernetes_agent": "deployment env has FAULT_INJECTION_ENABLED=true",
        },
        query="Investigate alert: InventorySlowQueries",
    )

    assert payload["specialists_invoked"] == ["metrics_agent"]
    assert payload["internal_evidence_sources"] == ["kubernetes_agent"]
    labels = [f["visible_label"] for f in payload["normalized_findings"]]
    assert labels == ["Prometheus Specialist"]


def test_the_narrator_is_never_handed_the_internal_agents_name():
    """The live leak: the findings block headed one section "Kubernetes Agent",
    and three supervisor handoffs in a row opened "Kubernetes Agent found
    FAULT_INJECTION_ENABLED=true" — in Slack, introducing the on-call to a
    teammate who is not in the room and cannot be asked anything. The name has
    to be absent from the prompt, not merely discouraged in it."""
    from sre_agent.narrative import _format_findings_block

    block = _format_findings_block(
        {
            "metrics_agent": "p90 latency is 2.179s",
            "kubernetes_agent": "deployment env has FAULT_INJECTION_ENABLED=true",
        }
    )

    assert "Prometheus Specialist" in block
    assert "FAULT_INJECTION_ENABLED=true" in block  # the evidence still lands
    lowered = block.lower()
    assert "kubernetes_agent" not in lowered
    assert "kubernetes agent" not in lowered
    assert "Cluster state" in block


def test_an_unregistered_agent_is_not_promoted_to_teammate():
    """Fail toward "a source" rather than inventing a colleague: a new internal
    agent added without a label entry must not become "Autoscaler Agent"."""
    from sre_agent.narrative import _format_findings_block

    block = _format_findings_block({"autoscaler_agent": "HPA scaled to 6 replicas"})

    assert "Autoscaler Agent" not in block
    assert "HPA scaled to 6 replicas" in block


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
