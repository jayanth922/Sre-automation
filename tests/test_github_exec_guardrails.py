#!/usr/bin/env python3
"""Unit tests for the github-exec MCP server guardrails."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "edge_mcp_servers" / "mcp_servers" / "github_exec" / "guardrails.py"
)
_spec = importlib.util.spec_from_file_location("github_exec_guardrails", _MODULE_PATH)
g = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = g
_spec.loader.exec_module(g)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("GITHUB_EXEC_ALLOWED_REPOS", "GITHUB_REPO"):
        monkeypatch.delenv(k, raising=False)


def test_revert_allowed_with_identifier(monkeypatch):
    monkeypatch.setenv("GITHUB_EXEC_ALLOWED_REPOS", "org/repo")
    ok, _ = g.guardrail_check("create_revert_pr", "org/repo", {"identifier": "abc123"})
    assert ok is True


def test_revert_requires_identifier(monkeypatch):
    monkeypatch.setenv("GITHUB_EXEC_ALLOWED_REPOS", "org/repo")
    ok, reason = g.guardrail_check("create_revert_pr", "org/repo", {})
    assert ok is False and "commit_sha or pr_number" in reason


def test_empty_allow_list_refuses_writes():
    ok, reason = g.guardrail_check("create_revert_pr", "org/repo", {"identifier": "abc"})
    assert ok is False
    assert "empty allow-list" in reason or "ALLOWED_REPOS" in reason


def test_disallowed_action_refused():
    ok, reason = g.guardrail_check("force_push", "org/repo", {})
    assert ok is False and "allow-list" in reason


def test_repo_allow_list(monkeypatch):
    monkeypatch.setenv("GITHUB_EXEC_ALLOWED_REPOS", "org/a, org/b")
    assert g.guardrail_check("comment_on_pr", "org/a", {"pr_number": 1})[0] is True
    assert g.guardrail_check("comment_on_pr", "org/c", {"pr_number": 1})[0] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
