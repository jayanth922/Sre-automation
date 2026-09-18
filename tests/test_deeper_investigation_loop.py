"""The reflector's deeper-investigation decision is a real bounded graph loop."""

import asyncio
import os

from sre_agent import graph_builder
from sre_agent.agent_state import ReflectorAnalysis


def _analysis(*, deeper=True, agents=None):
    return ReflectorAnalysis(
        hypothesis="metrics and logs disagree",
        confidence=0.4,
        reasoning="one evidence source is incomplete",
        unknowns=["whether errors continued after the rollout"],
        requires_deeper_investigation=deeper,
        recommended_agents=agents or [],
    )


def test_model_recommendations_are_mapped_to_a_fixed_agent_allowlist():
    assert graph_builder._validated_deeper_agents(
        ["metrics", "logs-agent", "metrics_agent", "planner", "../../shell"]
    ) == ["metrics_agent", "logs_agent"]


def test_deeper_investigation_is_bounded_by_the_durable_counter():
    analysis = _analysis(agents=["metrics_agent"])

    assert graph_builder._deeper_investigation_decision(analysis, 2, 3) == (
        "investigation_swarm",
        ["metrics_agent"],
    )
    assert graph_builder._deeper_investigation_decision(analysis, 3, 3) == (
        "planner",
        [],
    )


def test_invalid_or_unrequested_deeper_work_falls_through_to_planner():
    assert graph_builder._deeper_investigation_decision(
        _analysis(agents=["planner"]), 0, 3
    ) == ("planner", [])
    assert graph_builder._deeper_investigation_decision(
        _analysis(deeper=False, agents=["logs_agent"]), 0, 3
    ) == ("planner", [])
    assert graph_builder._route_reflector({"next": "arbitrary_model_node"}) == "planner"


def test_swarm_reruns_only_the_reflectors_selected_agents():
    calls = []

    def agent(name):
        async def run(state):
            calls.append((name, state["current_query"]))
            return {
                "agent_results": {name: f"fresh {name} evidence"},
                "thought_traces": state["thought_traces"],
            }

        return run

    node = graph_builder._make_investigation_swarm_node(
        agent("kubernetes_agent"),
        agent("metrics_agent"),
        agent("logs_agent"),
        agent("github_agent"),
    )
    out = asyncio.run(
        node(
            {
                "metadata": {
                    "deeper_investigation_agents": ["logs_agent"],
                    "cluster_namespace": "meridian",
                },
                "reflector_analysis": _analysis(agents=["logs_agent"]),
                "agent_results": {"metrics_agent": "original metrics evidence"},
                "thought_traces": {},
                "investigation_count": 1,
                "current_query": "investigate checkout errors",
            }
        )
    )

    assert [name for name, _query in calls] == ["logs_agent"]
    assert "remaining unknowns" in calls[0][1].lower()
    assert "meridian" in calls[0][1]
    assert out["agent_results"]["metrics_agent"] == "original metrics evidence"
    assert out["agent_results"]["logs_agent"] == "fresh logs_agent evidence"
    assert out["investigation_count"] == 2
    assert out["next"] == "reflector"


def test_reflector_emits_only_validated_checkpoint_safe_agent_names(monkeypatch):
    analysis = _analysis(agents=["prometheus", "planner", "logs-agent"])

    class FakeStructuredLlm:
        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return analysis

    from sre_agent import model_router

    monkeypatch.setattr(model_router, "route_llm", lambda *args, **kwargs: FakeStructuredLlm())
    out = asyncio.run(
        graph_builder._reflector_node(
            {
                "agent_results": {"metrics_agent": {"error_rate": 0.2}},
                "metadata": {"llm_provider": "openai"},
                "thought_traces": {},
                "investigation_count": 0,
            }
        )
    )

    assert out["next"] == "investigation_swarm"
    assert out["metadata"]["deeper_investigation_agents"] == [
        "metrics_agent",
        "logs_agent",
    ]


def test_graph_wires_the_reflector_loop_instead_of_claiming_a_dead_branch(monkeypatch):
    # Asserted against the compiled graph rather than the builder's source, so
    # that refactoring the builder cannot break the test while the wiring holds
    # — or, worse, leave it passing on source text after the wiring changes.
    monkeypatch.delenv("SENTINEL_ABLATION_ARM", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", "test"))
    graph = graph_builder.build_multi_agent_graph(
        tools=[], llm_provider="anthropic"
    ).get_graph()
    nodes = set(graph.nodes)
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert "investigation_swarm" in nodes
    assert ("investigation_swarm", "reflector") in edges
    # The reflector reaches the planner only through its conditional router,
    # never as an unconditional edge that would skip re-investigation.
    reflector_targets = {
        target for source, target in edges if source == "reflector"
    }
    assert {"investigation_swarm", "planner"} <= reflector_targets
    assert all(
        edge.conditional
        for edge in graph.edges
        if edge.source == "reflector" and edge.target == "planner"
    )
