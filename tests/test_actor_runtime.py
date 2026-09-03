#!/usr/bin/env python3
"""Unit tests for the actor runtime (LocalTerminalRuntime, the deterministic
Temporal-orchestrated actor)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.actor_runtime import (  # noqa: E402
    ActorResult,
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


def test_factory_returns_local_runtime():
    assert isinstance(get_agent_runtime(), LocalTerminalRuntime)


def test_get_agent_runtime_passes_workdir_through(tmp_path):
    rt = get_agent_runtime(workdir=str(tmp_path))
    assert isinstance(rt, LocalTerminalRuntime)
    assert rt.workdir == str(tmp_path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
