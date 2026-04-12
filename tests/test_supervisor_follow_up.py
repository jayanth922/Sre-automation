from types import SimpleNamespace
import uuid

import pytest

import sre_agent.supervisor as supervisor_module
from sre_agent.supervisor import SupervisorAgent


def _make_supervisor() -> SupervisorAgent:
    supervisor = SupervisorAgent.__new__(SupervisorAgent)
    supervisor.formatter = None
    supervisor.llm = None
    supervisor.system_prompt = ""
    return supervisor


@pytest.mark.asyncio
async def test_assistant_mode_returns_direct_answer(monkeypatch):
    captured_events = []

    async def fake_emit(*args, **kwargs):
        captured_events.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(supervisor_module, "emit_timeline_event", fake_emit)

    supervisor = _make_supervisor()
    state = {
        "current_query": "thanks",
        "metadata": {
            "conversation_mode": "assistant",
            "final_response": "## Summary\n- root cause: deploy regression",
        },
        "agents_invoked": [],
        "agent_results": {},
        "thought_traces": {},
        "incident_id": str(uuid.uuid4()),
        "final_response": None,
    }

    result = await supervisor.route(state)

    assert result["next"] == "FINISH"
    assert result["metadata"]["follow_up_mode"] == "direct"
    assert result["metadata"]["conversation_mode"] == "assistant"
    assert result["final_response"] == "Glad to help. If you want, I can also break down the evidence or next steps."

    aggregate_state = {
        **state,
        **result,
        "metadata": {**state["metadata"], **result["metadata"]},
    }

    aggregate_result = await supervisor.aggregate_responses(aggregate_state)

    assert aggregate_result["final_response"] == "Glad to help. If you want, I can also break down the evidence or next steps."
    assert captured_events[0]["kwargs"]["event_type"] == "assistant_message"
    assert captured_events[0]["kwargs"]["payload"]["mode"] == "direct_answer"


@pytest.mark.asyncio
async def test_assistant_mode_routes_code_change_question_to_github(monkeypatch):
    captured_events = []

    async def fake_emit(*args, **kwargs):
        captured_events.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(supervisor_module, "emit_timeline_event", fake_emit)

    supervisor = _make_supervisor()
    state = {
        "current_query": "What changed recently after the deploy?",
        "metadata": {
            "conversation_mode": "assistant",
            "final_response": "## Summary\n- root cause: release regression",
        },
        "agents_invoked": [],
        "agent_results": {},
        "thought_traces": {},
        "incident_id": str(uuid.uuid4()),
        "final_response": None,
    }

    result = await supervisor.route(state)

    assert result["next"] == "github_agent"
    assert result["metadata"]["follow_up_mode"] == "specialist"
    assert result["metadata"]["follow_up_specialist"] == "github_agent"
    assert captured_events[0]["kwargs"]["event_type"] == "decision"
    assert captured_events[0]["kwargs"]["payload"]["next_agent"] == "github_agent"