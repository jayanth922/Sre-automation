#!/usr/bin/env python3
"""Structural tests for the Terminal-Bench adapter (no terminal-bench needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "terminal_bench_adapter.py"
_spec = importlib.util.spec_from_file_location("terminal_bench_adapter", _MODULE_PATH)
tba = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tba
_spec.loader.exec_module(tba)


def test_agent_name():
    assert tba.SRETerminalAgent.name() == "sre-terminal-agent"


def test_env_includes_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    env = tba.SRETerminalAgent()._env()
    assert env["LLM_PROVIDER"] == "anthropic"
    assert env["ANTHROPIC_API_KEY"] == "secret"


def test_run_commands_include_and_escape_instruction():
    cmds = tba.SRETerminalAgent()._run_agent_commands("fix the build; it's broken")
    assert len(cmds) == 1
    assert "--task" in cmds[0]
    assert "sre_agent.terminal_agent" in cmds[0]  # points at the real agent module
    assert "'\\''" in cmds[0]  # single quote escaped for the shell


def test_install_script_written(tmp_path, monkeypatch):
    target = tmp_path / "install.sh"
    monkeypatch.setenv("TB_INSTALL_SCRIPT_PATH", str(target))
    path = tba.SRETerminalAgent()._install_agent_script_path()
    assert Path(path).exists()
    assert "installed" in Path(path).read_text()


def test_harbor_available_is_bool():
    assert isinstance(tba.harbor_available(), bool)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
