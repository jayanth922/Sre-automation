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
    for k in (
        "EXECUTOR_ALLOWED_NAMESPACES",
        "EXECUTOR_MIN_REPLICAS",
        "EXECUTOR_ALLOWED_ENV_KEYS",
        "EXECUTOR_MAX_ENV_KEYS",
        "EXECUTOR_MAX_ENV_VALUE_CHARS",
    ):
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


def test_env_patch_with_plain_flags_ok():
    ok, _ = guardrail_check(
        "patch_deployment_env",
        "demo-app",
        {"env": {"LOG_LEVEL": "debug", "SLOW_QUERY_RATE": "0"}},
    )
    assert ok is True


def test_env_patch_without_env_refused():
    ok, reason = guardrail_check("patch_deployment_env", "demo-app", {})
    assert ok is False and "non-empty" in reason


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_PASSWORD",
        "STRIPE_API_KEY",
        "JWT_SECRET",
        "SESSION_TOKEN",
        "TLS_CERT",
        "signing_key",
    ],
)
def test_credential_named_env_var_is_never_settable(key):
    """The denylist is the last line against a prompt-injected 'set the password'."""
    ok, reason = guardrail_check("patch_deployment_env", "demo-app", {"env": {key: "x"}})
    assert ok is False and "credential" in reason


def test_invalid_env_var_name_refused():
    ok, reason = guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"not a name": "x"}}
    )
    assert ok is False and "valid environment variable name" in reason


def test_non_scalar_env_value_refused():
    ok, reason = guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"FLAGS": {"a": 1}}}
    )
    assert ok is False and "scalar" in reason


def test_key_count_cap_is_a_blast_radius_guard(monkeypatch):
    monkeypatch.setenv("EXECUTOR_MAX_ENV_KEYS", "2")
    ok, reason = guardrail_check(
        "patch_deployment_env",
        "demo-app",
        {"env": {"A": "1", "B": "2", "C": "3"}},
    )
    assert ok is False and "blast-radius" in reason


def test_value_length_cap(monkeypatch):
    monkeypatch.setenv("EXECUTOR_MAX_ENV_VALUE_CHARS", "8")
    assert guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"A": "123456789"}}
    )[0] is False
    assert guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"A": "1234"}}
    )[0] is True


def test_operator_narrowing_allow_list(monkeypatch):
    monkeypatch.setenv("EXECUTOR_ALLOWED_ENV_KEYS", "LOG_LEVEL, SLOW_QUERY_RATE")
    assert guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"LOG_LEVEL": "debug"}}
    )[0] is True
    ok, reason = guardrail_check(
        "patch_deployment_env", "demo-app", {"env": {"CACHE_TTL": "60"}}
    )
    assert ok is False and "allow-list" in reason


def test_env_patch_still_namespace_scoped():
    ok, reason = guardrail_check(
        "patch_deployment_env", "kube-system", {"env": {"LOG_LEVEL": "debug"}}
    )
    assert ok is False and "namespace" in reason


def test_read_only_config_dump_is_allow_listed():
    assert guardrail_check("get_deployment_config", "demo-app")[0] is True
    assert guardrail_check("get_deployment_config", "kube-system")[0] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
