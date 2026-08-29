#!/usr/bin/env python3
"""Tests for github-exec truthful revert outcomes (R11)."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SERVER = _ROOT / "edge_mcp_servers" / "mcp_servers" / "github_exec"


@pytest.fixture()
def github_exec(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "acme/demo")
    monkeypatch.setenv("GITHUB_EXEC_ALLOWED_REPOS", "acme/demo")
    monkeypatch.delenv("GITHUB_EXEC_REVERT_LABEL", raising=False)

    # Stub FastMCP so importing server.py does not need the mcp package.
    class _FakeMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def decorator(fn):
                return fn

            return decorator

    fake_mcp = SimpleNamespace(FastMCP=_FakeMCP)
    sys.modules["mcp"] = SimpleNamespace(server=SimpleNamespace(fastmcp=fake_mcp))
    sys.modules["mcp.server"] = SimpleNamespace(fastmcp=fake_mcp)
    sys.modules["mcp.server.fastmcp"] = fake_mcp

    sys.path.insert(0, str(_SERVER))
    for name in ("guardrails", "server"):
        sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location("github_exec_server_r11", _SERVER / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["github_exec_server_r11"] = module
    spec.loader.exec_module(module)
    module.REPO = "acme/demo"
    return module


@pytest.mark.asyncio
async def test_create_revert_pr_dry_run(github_exec):
    raw = await github_exec.create_revert_pr("42", dry_run=True)
    payload = json.loads(raw)
    assert payload["status"] == "DRY_RUN"
    assert payload["applied"] is False


@pytest.mark.asyncio
async def test_create_revert_pr_created(github_exec, monkeypatch):
    def fake_gh(args):
        assert args[:3] == ["pr", "revert", "42"]
        return SimpleNamespace(
            returncode=0,
            stdout="https://github.com/acme/demo/pull/99\n",
            stderr="",
        )

    monkeypatch.setattr(github_exec, "_gh", fake_gh)
    raw = await github_exec.create_revert_pr("42", dry_run=False)
    payload = json.loads(raw)
    assert payload["status"] == "CREATED"
    assert payload["applied"] is True
    assert payload["pr_url"] == "https://github.com/acme/demo/pull/99"


@pytest.mark.asyncio
async def test_create_revert_pr_manual_for_sha(github_exec):
    raw = await github_exec.create_revert_pr("abc123def", dry_run=False)
    payload = json.loads(raw)
    assert payload["status"] == "MANUAL_REQUIRED"
    assert payload["applied"] is False
    assert "pr_url" not in payload or not payload.get("pr_url")


@pytest.mark.asyncio
async def test_create_revert_pr_workflow_not_created(github_exec, monkeypatch):
    monkeypatch.setenv("GITHUB_EXEC_REVERT_LABEL", "needs-revert")
    calls = []

    def fake_gh(args):
        calls.append(args)
        if args[1] == "revert":
            return SimpleNamespace(returncode=1, stdout="", stderr="cannot revert")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(github_exec, "_gh", fake_gh)
    raw = await github_exec.create_revert_pr("7", dry_run=False)
    payload = json.loads(raw)
    assert payload["status"] == "WORKFLOW_TRIGGERED"
    assert payload["applied"] is False
    assert payload.get("pr_url") is None


def test_guardrail_requires_allowlist(monkeypatch):
    sys.path.insert(0, str(_SERVER))
    sys.modules.pop("guardrails", None)
    spec = importlib.util.spec_from_file_location("github_exec_guardrails_r11", _SERVER / "guardrails.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)

    monkeypatch.delenv("GITHUB_EXEC_ALLOWED_REPOS", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    ok, reason = g.guardrail_check("create_revert_pr", "", {"identifier": "1"})
    assert ok is False
    assert "GITHUB_REPO" in reason

    monkeypatch.setenv("GITHUB_REPO", "acme/demo")
    monkeypatch.setenv("GITHUB_EXEC_ALLOWED_REPOS", "other/repo")
    ok, reason = g.guardrail_check("create_revert_pr", "acme/demo", {"identifier": "1"})
    assert ok is False
    assert "allow-list" in reason
