#!/usr/bin/env python3
"""Structural tests for the AIOpsLab adapter (Phase 5, no aiopslab package needed)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "aiopslab_adapter.py"
_spec = importlib.util.spec_from_file_location("aiopslab_adapter", _MODULE_PATH)
aio = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = aio
_spec.loader.exec_module(aio)


# ── build_submit_action ──────────────────────────────────────────────────────
def test_detection_submits_yes_when_root_cause_found():
    act_report = {"resolution_report": {"root_cause": "OOM in checkout-service"}}
    action = aio.build_submit_action("detection", act_report)
    assert action == '```\nsubmit("Yes")\n```'


def test_detection_submits_no_when_nothing_found():
    action = aio.build_submit_action("detection", {})
    assert action == '```\nsubmit("No")\n```'


def test_localization_submits_service_list_deduped():
    act_report = {
        "action_reports": [
            {"action_type": "rollback", "target": "checkout-service"},
            {"action_type": "restart", "target": "checkout-service"},
            {"action_type": "scale", "target": "inventory-service"},
        ]
    }
    action = aio.build_submit_action("localization", act_report)
    assert action == '```\nsubmit(["checkout-service", "inventory-service"])\n```'


def test_localization_submits_empty_list_with_no_targets():
    action = aio.build_submit_action("localization", {})
    assert action == "```\nsubmit([])\n```"


def test_analysis_submits_system_level_and_fault_type():
    act_report = {
        "resolution_report": {
            "root_cause": "bad config push",
            "system_level": "Application",
            "fault_type": "Misconfiguration",
        }
    }
    action = aio.build_submit_action("analysis", act_report)
    assert action == (
        '```\nsubmit({"system_level": "Application", "fault_type": "Misconfiguration"})\n```'
    )


def test_analysis_submits_empty_when_no_fault():
    action = aio.build_submit_action("analysis", {})
    assert action == "```\nsubmit()\n```"


def test_mitigation_submit_takes_no_params():
    action = aio.build_submit_action("mitigation", {"resolution_report": {"root_cause": "x"}})
    assert action == "```\nsubmit()\n```"


def test_unknown_task_type_rejected():
    with pytest.raises(ValueError):
        aio.build_submit_action("bogus", {})


# ── build_mitigation_shell_actions ──────────────────────────────────────────
def test_mitigation_shell_actions_replay_executed_commands():
    act_report = {
        "executed": [
            {"command": "kubectl rollout undo deployment/checkout-service -n prod"},
            {"command": "# no command mapping for action_type='escalate' on 'x'"},
        ]
    }
    actions = aio.build_mitigation_shell_actions(act_report)
    assert actions == [
        '```\nexec_shell("kubectl rollout undo deployment/checkout-service -n prod")\n```'
    ]


def test_mitigation_shell_actions_falls_back_to_action_reports():
    act_report = {"action_reports": [{"command": "kubectl scale deployment/inv --replicas=3 -n prod"}]}
    actions = aio.build_mitigation_shell_actions(act_report)
    assert len(actions) == 1
    assert "kubectl scale" in actions[0]


# ── SREAIOpsLabAgent ─────────────────────────────────────────────────────────
def test_agent_queues_mitigation_shell_then_submit():
    async def agent_invoke(ctx):
        return {
            "act_report": {
                "resolution_report": {"root_cause": "bad deploy"},
                "executed": [{"command": "kubectl rollout undo deployment/svc -n prod"}],
            },
            "summary": "rolled back",
        }

    async def scenario():
        agent = aio.SREAIOpsLabAgent("mitigation", agent_invoke)
        agent.init_context("desc", "instructions", {"exec_shell(cmd)": "docs", "submit()": "docs"})
        first = await agent.get_action("Please take the next action")
        second = await agent.get_action("ok")
        third = await agent.get_action("ok")
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert "exec_shell" in first
    assert second == "```\nsubmit()\n```"
    assert third == "```\nsubmit()\n```"  # queue exhausted -> safe fallback


def test_agent_invokes_pipeline_only_once():
    calls = []

    async def agent_invoke(ctx):
        calls.append(ctx)
        return {"act_report": {"resolution_report": {"root_cause": "OOM"}}, "summary": ""}

    async def scenario():
        agent = aio.SREAIOpsLabAgent("detection", agent_invoke)
        await agent.get_action("state1")
        await agent.get_action("state2")

    asyncio.run(scenario())
    assert len(calls) == 1


def test_unknown_task_type_rejected_by_agent():
    async def agent_invoke(ctx):
        return {}

    with pytest.raises(ValueError):
        aio.SREAIOpsLabAgent("bogus", agent_invoke)


# ── from_aiopslab_run ────────────────────────────────────────────────────────
def test_from_aiopslab_run_counts_assistant_turns_and_normalizes_results():
    output = {
        "history": [
            {"role": "assistant", "content": "..."},
            {"role": "env", "content": "..."},
            {"role": "assistant", "content": "..."},
        ],
        "final_state": "VALID_SUBMISSION",
        "results": {"TTM": 42.5, "success": True},
    }
    result = aio.from_aiopslab_run("k8s_target_port-misconfig-mitigation-1", "mitigation", output)
    assert result.steps_taken == 2
    assert result.results == {"TTM": 42.5, "success": True}
    assert result.final_state == "VALID_SUBMISSION"
    assert result.to_dict()["problem_id"] == "k8s_target_port-misconfig-mitigation-1"


# ── availability / run_problem guard ────────────────────────────────────────
def test_aiopslab_available_is_bool():
    assert isinstance(aio.aiopslab_available(), bool)


def test_run_problem_raises_clearly_without_aiopslab():
    if aio.aiopslab_available():
        pytest.skip("aiopslab is installed in this environment")

    async def agent_invoke(ctx):
        return {}

    async def scenario():
        await aio.run_problem("some-problem", "detection", agent_invoke)

    with pytest.raises(RuntimeError, match="aiopslab is not installed"):
        asyncio.run(scenario())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
