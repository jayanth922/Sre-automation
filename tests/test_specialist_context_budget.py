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

import inspect
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

from sre_agent import agent_nodes, context_compaction
from sre_agent.constants import AgentMetadata


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


def test_logs_prompt_uses_real_bounded_loki_tool_contract():
    prompt_path = (
        Path(agent_nodes.__file__).parent / "config" / "prompts" / "logs_agent_prompt.txt"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "query_logs(logql=" in prompt
    assert "start_time=" in prompt and "end_time=" in prompt
    assert "limit=100" in prompt
    assert "analyze_log_patterns(logql=" in prompt
    assert "analyze_log_patterns" in prompt and "start_time=" in prompt
    assert "target-free selectors" in prompt
    assert "search_logs" not in prompt


def test_specialist_prompts_match_their_real_bounded_tool_contracts():
    prompt_dir = Path(agent_nodes.__file__).parent / "config" / "prompts"

    metrics = (prompt_dir / "metrics_agent_prompt.txt").read_text(encoding="utf-8")
    for tool_name in ("get_golden_signals", "get_metric_range", "get_metric"):
        assert f"{tool_name}(" in metrics
    for stale_name in (
        "get_performance_metrics",
        "get_resource_metrics",
        "analyze_trends",
    ):
        assert stale_name not in metrics
    assert 'time="<alert timestamp>"' in metrics
    assert "Every PromQL query must include the affected target selector" in metrics

    github = (prompt_dir / "github_agent_prompt.txt").read_text(encoding="utf-8")
    assert "since=<alert-2h>" in github
    assert "until=<alert+15m>" in github
    assert "limit=20" in github
    assert "at most the three" in github
    assert "list_repository_files" not in github
    assert "get_repository_file" not in github
    # The prompt must describe what github_real actually returns. `get_commit`
    # was advertised as returning a diff while the handler returned
    # `"diff":null`; it now returns ranked, budget-bounded file rows, and the
    # loss flags only help if the prompt names them.
    assert "list_commits(since, until, path, limit)" in github
    assert "largest change first" in github
    for flag in ("files_omitted", "patch_chars_omitted", "files_scan_truncated"):
        assert flag in github
    assert "Never conclude that a file was" in github

    kubernetes = (prompt_dir / "kubernetes_agent_prompt.txt").read_text(
        encoding="utf-8"
    )
    assert 'label_selector="app=<exact service>", limit=20' in kubernetes
    assert "involved_object_name" in kubernetes and "limit=50" in kubernetes
    assert "tail_lines=100" in kubernetes
    assert "namespace scope is missing" in kubernetes
    assert "`production`, `default`" in kubernetes

    runbooks = (prompt_dir / "runbooks_agent_prompt.txt").read_text(
        encoding="utf-8"
    )
    assert 'alert_name="<exact alert>"' in runbooks
    assert "do not conduct a second open-ended search" in runbooks


def test_specialist_tool_catalog_excludes_broad_or_unavailable_reads():
    config_path = Path(agent_nodes.__file__).parent / "config" / "agent_config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    agents = config["agents"]

    assert agents["runbooks_agent"]["tools"] == [
        "search_runbooks",
        "get_runbook_content",
    ]
    assert not {
        "get_incident_playbook",
        "get_troubleshooting_guide",
        "get_escalation_procedures",
        "get_common_resolutions",
    }.intersection(agents["runbooks_agent"]["tools"])

    broad_kubernetes_tools = {
        "list_namespaces",
        "list_services",
        "list_deployments",
        "get_node_status",
    }
    assert not broad_kubernetes_tools.intersection(
        agents["kubernetes_agent"]["tools"]
    )
    assert not broad_kubernetes_tools.intersection(agents["single_agent"]["tools"])


@pytest.mark.asyncio
async def test_specialist_persists_fit_summary_in_state_metadata(monkeypatch):
    captured = {}

    class _Agent:
        async def astream(self, payload, config):
            hook = captured["pre_model_hook"]
            hook(payload)
            hook(
                {
                    "messages": [
                        *payload["messages"],
                        {
                            "role": "ai",
                            "content": "",
                            "tool_calls": [
                                {"id": "t1", "name": "query_logs", "args": {}}
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "t1",
                            "content": "L" * 40000,
                        },
                    ]
                }
            )
            # Emitted on the stream as well as into the hook: a lane that
            # yields no tool call at all is treated as having gathered no
            # evidence and is asked again, which is not what this test is
            # measuring.
            yield {
                "agent": {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"id": "t1", "name": "query_logs", "args": {}}
                            ],
                        )
                    ]
                }
            }
            yield {"agent": {"messages": [AIMessage(content="done")]}}

    def _fake_create_react_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return _Agent()

    async def _fake_artifact(state, **kwargs):
        return dict(state.get("metadata", {}) or {}), None

    async def _no_narration(*args, **kwargs):
        return ""

    async def _no_event(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_nodes, "create_react_agent", _fake_create_react_agent)
    monkeypatch.setattr(agent_nodes, "_create_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent_nodes, "_artifact_backed_trace_metadata", _fake_artifact)
    monkeypatch.setattr(agent_nodes, "narrate_specialist_finding", _no_narration)
    monkeypatch.setattr(agent_nodes, "emit_timeline_event", _no_event)

    node = agent_nodes.BaseAgentNode(
        name="logs",
        description="logs",
        tools=[],
        llm_provider="openai",
        agent_metadata=AgentMetadata(
            actor_id="logs-agent",
            display_name="Application Logs Agent",
            description="Reads logs",
            agent_type="logs",
        ),
    )
    result = await node(
        {
            "current_query": "Investigate",
            "alert_context": {"alert_name": "A", "annotations": {}},
            "metadata": {},
            "agent_results": {},
            "agent_tool_failures": {},
            "agents_invoked": [],
            "thought_traces": {},
        }
    )

    summary = result["metadata"]["context_fitting"]["logs_agent"]
    assert summary["specialist_invocations"] == 1
    assert summary["model_calls"] == 2
    assert summary["changed_calls"] == 1
    assert summary["estimated_message_tokens_avoided"] > 0


def test_saas_job_result_persists_fit_telemetry_on_success_and_failure():
    """A process-local log cannot support a later cost comparison."""
    from sre_agent import agent_runtime

    source = inspect.getsource(agent_runtime._run_graph_impl)
    assert '"context_fitting": context_fitting' in source
    # The failure payload reads the latest graph metadata independently; it
    # cannot rely on the success-only local variable above.
    failure_section = source[source.index("except Exception as e:") :]
    assert '"context_fitting": dict(' in failure_section


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
