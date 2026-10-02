"""Specialists run the model the cluster's Settings chose, not a fixed default."""

from sre_agent import agent_nodes, model_router
from sre_agent.model_router import TaskType, select_model


def test_the_settings_model_anchors_the_specialist_route(monkeypatch):
    seen = {}

    def fake_route_llm(task_type, **kwargs):
        seen.update(kwargs, task_type=task_type)
        return object()

    monkeypatch.setattr(model_router, "route_llm", fake_route_llm)
    agent_nodes._create_llm(
        "anthropic", router_enabled=True, model_id="claude-sonnet-4-5"
    )
    assert seen["task_type"] is TaskType.SPECIALIST
    assert seen["anchor_model"] == "claude-sonnet-4-5"


def test_a_specialist_with_an_anchor_gets_that_model_not_the_default():
    decision = select_model(
        TaskType.SPECIALIST, router_enabled=True, anchor_model="claude-sonnet-4-5"
    )
    assert decision.model_id == "claude-sonnet-4-5"


def test_a_specialist_with_no_settings_model_keeps_the_fixed_default():
    decision = select_model(TaskType.SPECIALIST, router_enabled=True)
    assert decision.model_id == "claude-sonnet-5"
