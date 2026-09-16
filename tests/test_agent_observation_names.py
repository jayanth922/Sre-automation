"""Specialist subgraphs keep distinct, stable Langfuse observation names."""

from sre_agent import agent_nodes
from sre_agent.constants import AgentMetadata


def test_react_subagent_uses_concrete_role_name(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        agent_nodes,
        "_create_llm",
        lambda *_args, **_kwargs: object(),
    )

    def fake_create_react_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        agent_nodes,
        "create_react_agent",
        fake_create_react_agent,
    )

    agent_nodes.BaseAgentNode(
        name="fallback",
        description="fallback",
        tools=[],
        llm_provider="openai",
        agent_metadata=AgentMetadata(
            actor_id="metrics-agent",
            display_name="Performance Metrics Agent",
            description="Reads performance telemetry",
            agent_type="metrics",
        ),
    )

    assert captured["name"] == "metrics_agent"
    assert captured["name"] != "agent"


def test_specialist_trace_metadata_carries_stable_internal_role_name():
    assert agent_nodes.specialist_trace_metadata("metrics") == {
        "sentinel.specialist_role": "metrics_agent"
    }
