#!/usr/bin/env python3
"""Unit tests for the Executor MCP server edge-side guardrails.

Loads the pure ``guardrails.py`` module directly (no kubernetes/mcp deps) so it
runs in any environment.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "edge_mcp_servers" / "mcp_servers" / "executor_real" / "guardrails.py"
)
_spec = importlib.util.spec_from_file_location("executor_guardrails", _MODULE_PATH)
guardrails = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guardrails
_spec.loader.exec_module(guardrails)

guardrail_check = guardrails.guardrail_check


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("EXECUTOR_ALLOWED_NAMESPACES", "EXECUTOR_MIN_REPLICAS"):
        monkeypatch.delenv(k, raising=False)


def test_restart_in_allowed_namespace_ok():
    ok, _ = guardrail_check("restart", "demo-app")
    assert ok is True


def test_disallowed_namespace_refused():
    ok, reason = guardrail_check("restart", "kube-system")
    assert ok is False and "allow-list" in reason


def test_unknown_action_refused():
    ok, reason = guardrail_check("delete", "demo-app")
    assert ok is False and "allow-list" in reason


def test_scale_to_zero_refused():
    ok, reason = guardrail_check("scale", "demo-app", {"replicas": 0})
    assert ok is False and "floor" in reason


def test_scale_above_floor_ok():
    ok, _ = guardrail_check("scale", "demo-app", {"replicas": 3})
    assert ok is True


def test_scale_missing_replicas_refused():
    ok, reason = guardrail_check("scale", "demo-app", {})
    assert ok is False and "replicas" in reason


def test_custom_namespace_allow_list(monkeypatch):
    monkeypatch.setenv("EXECUTOR_ALLOWED_NAMESPACES", "prod-a, prod-b")
    assert guardrail_check("restart", "prod-a")[0] is True
    assert guardrail_check("restart", "demo-app")[0] is False


def test_custom_min_replicas_floor(monkeypatch):
    monkeypatch.setenv("EXECUTOR_MIN_REPLICAS", "2")
    assert guardrail_check("scale", "demo-app", {"replicas": 1})[0] is False
    assert guardrail_check("scale", "demo-app", {"replicas": 2})[0] is True


def test_recreate_pod_in_allowed_namespace_ok():
    ok, _ = guardrail_check("recreate_pod", "demo-app")
    assert ok is True


def test_recreate_pod_disallowed_namespace_refused():
    ok, reason = guardrail_check("recreate_pod", "kube-system")
    assert ok is False and "allow-list" in reason


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
