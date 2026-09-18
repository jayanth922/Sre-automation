#!/usr/bin/env python3
"""A specialist's ReAct loop budgets its own context before every model call.

Compaction used to run in exactly one place: `agent_runtime._maybe_compact`,
pre-run, over `state["messages"]`. That is the one list a specialist never
touches. Each specialist builds an isolated `[system_message, user_message]`
pair on purpose (to keep another specialist's `tool_calls` out of its history)
and deliberately does not return `messages`, so its whole multi-turn tool loop
— the part that accumulates `kubectl get -o json` payloads by the tens of
thousands of tokens and re-sends all of them on every iteration — grew with no
budget at all. The run died at the provider, mid-investigation, on the long
incidents that needed it most.

These tests hold the wiring: the hook is installed on the loop, it trims, and
it trims only the model's view.
"""

from __future__ import annotations

import pytest

from sre_agent import agent_nodes, context_compaction


@pytest.fixture
def tiny_budget(monkeypatch):
    """A 2000-token input ceiling, with deterministic counting."""
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "6000")
    monkeypatch.setenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN_RATIO", "0")
    monkeypatch.setenv("CONTEXT_TOKENIZER", "heuristic")
    monkeypatch.setenv("CONTEXT_TOKEN_SAFETY_RATIO", "1.0")
    monkeypatch.delenv("CONTEXT_ITERATION_MAX_TOKENS", raising=False)
    context_compaction.reset_tokenizer_cache()
    yield 2000
    context_compaction.reset_tokenizer_cache()


@pytest.fixture
def captured_kwargs(monkeypatch):
    """Build a specialist without an API key and capture its agent wiring."""
    captured: dict = {}

    def _fake_create_react_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent_nodes, "create_react_agent", _fake_create_react_agent)
    monkeypatch.setattr(agent_nodes, "_create_llm", lambda *a, **k: object())
    agent_nodes.BaseAgentNode(name="test-specialist", description="d", tools=[])
    return captured


def test_react_loop_is_built_with_a_context_budget_hook(captured_kwargs):
    assert callable(captured_kwargs.get("pre_model_hook")), (
        "specialists must budget context per iteration; without pre_model_hook "
        "the tool loop grows unbounded and nothing else ever sees it"
    )


def test_the_hook_trims_an_oversized_tool_transcript(captured_kwargs, tiny_budget):
    hook = captured_kwargs["pre_model_hook"]
    messages = [{"role": "system", "content": "you are an infra specialist"}]
    for i in range(5):
        messages += [
            {
                "role": "ai",
                "content": "",
                "tool_calls": [{"id": f"t{i}", "name": "kubectl", "args": {"q": "pods"}}],
            },
            {"role": "tool", "content": "P" * 30000, "tool_call_id": f"t{i}"},
        ]
    messages.append({"role": "user", "content": "what is wrong?"})

    assert context_compaction.messages_tokens(messages) > tiny_budget
    fitted = hook({"messages": messages})["llm_input_messages"]
    assert context_compaction.messages_tokens(fitted) <= tiny_budget


def test_the_hook_leaves_graph_state_intact(captured_kwargs, tiny_budget):
    """Trimming the prompt must not trim the evidence.

    `tool_failures` is keyed off `ToolMessage.status` in the real transcript and
    the specialist's full tool trace is persisted as an evidence artifact. Both
    read graph state, which is why this returns `llm_input_messages` rather
    than rewriting `messages`.
    """
    payload = "P" * 30000
    state = {
        "messages": [
            {"role": "system", "content": "prompt"},
            {
                "role": "ai",
                "content": "",
                "tool_calls": [{"id": "t1", "name": "kubectl", "args": {}}],
            },
            {"role": "tool", "content": payload, "tool_call_id": "t1"},
        ]
    }
    out = captured_kwargs["pre_model_hook"](state)

    assert set(out) == {"llm_input_messages"}
    assert state["messages"][2]["content"] == payload
    assert len(out["llm_input_messages"][2]["content"]) < len(payload)


def test_the_hook_is_a_noop_on_a_short_history(captured_kwargs):
    """Normal-length investigations pay nothing for this."""
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "pods are crashlooping"},
    ]
    assert captured_kwargs["pre_model_hook"]({"messages": messages})[
        "llm_input_messages"
    ] == messages


def test_pre_run_compaction_falls_back_to_a_deterministic_fit(tiny_budget, monkeypatch):
    """A failed summarizer must not hand an over-window history to the provider.

    Summarization is itself a model call, so it fails precisely when the
    provider is degraded. The old code logged "compaction skipped (non-fatal)"
    and sent the uncompacted history anyway — turning a recoverable blip into a
    dead run on the longest investigations.
    """
    import asyncio

    from sre_agent import agent_runtime

    def _no_llm(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("sre_agent.model_router.route_llm", _no_llm)

    state = {
        "messages": [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "H" * 40000},
            {"role": "ai", "content": "H" * 40000},
        ]
    }
    out = asyncio.run(agent_runtime._maybe_compact(state))
    assert context_compaction.messages_tokens(out["messages"]) <= tiny_budget


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
