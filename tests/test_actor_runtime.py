#!/usr/bin/env python3
"""Unit tests for the pluggable actor runtime (project #2: Hermes backend)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.actor_runtime import (  # noqa: E402
    ActorResult,
    HermesRuntime,
    LocalTerminalRuntime,
    get_agent_runtime,
)


def _scripted_decider(task, history):
    if history:
        return {"action": "done", "success": True, "reason": "done"}
    return {"action": "run", "command": "echo hi"}


def test_local_runtime_runs_terminal_agent(tmp_path):
    rt = LocalTerminalRuntime(decider=_scripted_decider, workdir=str(tmp_path), max_steps=5)
    res = rt.run("say hi")
    assert isinstance(res, ActorResult)
    assert res.backend == "local-terminal"
    assert res.status == "SOLVED"
    assert "echo hi" in res.output


def test_factory_selects_local_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    assert isinstance(get_agent_runtime(), LocalTerminalRuntime)


def test_factory_selects_hermes(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "hermes")
    assert isinstance(get_agent_runtime(), HermesRuntime)


def test_factory_openclaw_maps_to_hermes():
    assert isinstance(get_agent_runtime("openclaw"), HermesRuntime)


def test_hermes_runtime_raises_clear_error_without_package():
    # hermes-agent isn't installed here → run() must fail with an install hint,
    # not a cryptic ImportError.
    with pytest.raises(RuntimeError, match="hermes-agent not installed"):
        HermesRuntime().run("do something")


def test_hermes_runtime_construction_does_not_import_package():
    # Constructing the backend must not require the package (only run() does).
    rt = HermesRuntime(model="anthropic/claude-sonnet-4.6", max_iterations=10)
    assert rt.name == "hermes"
    assert rt.max_iterations == 10


def test_hermes_runtime_accepts_workdir(tmp_path):
    # Phase F (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md): generate_patch_activity
    # always passes workdir= regardless of backend — HermesRuntime must accept
    # it (previously missing entirely).
    rt = HermesRuntime(workdir=str(tmp_path))
    assert rt.workdir == str(tmp_path)


def test_get_agent_runtime_passes_workdir_through_to_local(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "local")
    rt = get_agent_runtime(workdir=str(tmp_path))
    assert isinstance(rt, LocalTerminalRuntime)
    assert rt.workdir == str(tmp_path)


def test_get_agent_runtime_passes_task_id_to_hermes_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "hermes")
    rt = get_agent_runtime(workdir=str(tmp_path), task_id="sre-actor-org1-inc1")
    assert isinstance(rt, HermesRuntime)
    assert rt.task_id == "sre-actor-org1-inc1"

    # LocalTerminalRuntime has no task_id concept — must not be passed through
    # (would raise TypeError if it were).
    monkeypatch.setenv("AGENT_RUNTIME", "local")
    rt = get_agent_runtime(workdir=str(tmp_path), task_id="sre-actor-org1-inc1")
    assert isinstance(rt, LocalTerminalRuntime)


def test_hermes_runtime_fails_closed_when_package_rejects_workdir(monkeypatch, tmp_path):
    # hermes-agent's documented API has no workdir/sandbox parameter, so
    # AIAgent(workdir=...) is expected to raise TypeError. HermesRuntime must
    # fail closed (raise) rather than silently retry unconfined.
    class _FakeAIAgent:
        def __init__(self, **kwargs):
            if "workdir" in kwargs:
                raise TypeError("unexpected keyword argument 'workdir'")

    fake_module = type(sys)("run_agent")
    fake_module.AIAgent = _FakeAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)

    rt = HermesRuntime(workdir=str(tmp_path))
    with pytest.raises(RuntimeError, match="refusing to run unconfined"):
        rt._build_agent()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
