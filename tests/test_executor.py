#!/usr/bin/env python3
"""Unit tests for the dry-run Executor (ACT phase)."""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.executor import (  # noqa: E402
    Executor,
    build_command,
    build_rollback_command,
)


@dataclass
class FakeAction:
    action_type: str
    target: str = "checkout-service"
    parameters: Dict[str, Any] = field(default_factory=dict)
    rollback_plan: Optional[str] = None


def test_build_command_restart():
    cmd = build_command(FakeAction("restart", parameters={"namespace": "demo-app"}))
    assert cmd == "kubectl rollout restart deployment/checkout-service -n demo-app"


def test_build_command_scale_uses_replicas():
    cmd = build_command(FakeAction("scale", parameters={"replicas": 4, "namespace": "demo-app"}))
    assert "--replicas=4" in cmd
    assert "-n demo-app" in cmd


def test_build_command_rollback():
    cmd = build_command(FakeAction("rollback"))
    assert cmd == "kubectl rollout undo deployment/checkout-service -n default"


def test_build_command_revert_commit_mentions_sha():
    cmd = build_command(FakeAction("revert_commit", parameters={"commit_sha": "abc123"}))
    assert "abc123" in cmd


def test_build_command_escalate_is_noop_notify():
    cmd = build_command(FakeAction("escalate"))
    assert "no infrastructure mutation" in cmd


def test_dry_run_returns_command_and_audit_hash():
    ex = Executor(actor="sre-agent", incident_id="inc-1")
    result = ex.execute(FakeAction("restart"), gate_decision="autonomous", dry_run=True)
    assert result.status == "DRY_RUN"
    assert result.command.startswith("kubectl rollout restart")
    assert result.audit["gate_decision"] == "autonomous"
    assert len(result.audit["content_hash"]) == 64  # sha256 hex


def test_audit_hash_detects_tampering():
    ex = Executor()
    result = ex.execute(FakeAction("restart"), gate_decision="autonomous", dry_run=True)
    import hashlib, json
    record = {k: v for k, v in result.audit.items() if k != "content_hash"}
    record["target"] = "TAMPERED"
    recomputed = hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()
    assert recomputed != result.audit["content_hash"]


def test_live_execution_is_not_implemented_in_phase0():
    ex = Executor()
    with pytest.raises(NotImplementedError):
        ex.execute(FakeAction("restart"), gate_decision="autonomous", dry_run=False)


def test_rollback_command_for_scale():
    rb = build_rollback_command(FakeAction("scale", parameters={"replicas": 4}))
    assert rb is not None and "replicas=<previous>" in rb


# ── Live path (aexecute) ────────────────────────────────────────────────────

def test_aexecute_dry_run_matches_sync():
    ex = Executor()
    res = asyncio.run(ex._aexecute_unchecked(FakeAction("restart"), "autonomous", dry_run=True))
    assert res.status == "DRY_RUN"


def test_aexecute_live_calls_tool_caller_with_mapped_tool():
    calls = {}

    async def fake_caller(tool_name, args):
        calls["tool"] = tool_name
        calls["args"] = args
        return {"status": "OK", "tool": tool_name}

    ex = Executor()
    action = FakeAction("scale", parameters={"replicas": 3, "namespace": "demo-app"})
    res = asyncio.run(ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=fake_caller))
    assert res.status == "EXECUTED"
    assert calls["tool"] == "scale_deployment"
    assert calls["args"]["replicas"] == 3
    assert calls["args"]["dry_run"] is False


def test_aexecute_live_without_caller_is_error_not_silent():
    ex = Executor()
    res = asyncio.run(ex._aexecute_unchecked(FakeAction("restart"), "autonomous", dry_run=False, tool_caller=None))
    assert res.status == "ERROR"


def test_aexecute_unmapped_action_is_skipped():
    async def fake_caller(tool_name, args):
        return {}

    ex = Executor()
    res = asyncio.run(ex._aexecute_unchecked(FakeAction("escalate"), "autonomous", dry_run=False, tool_caller=fake_caller))
    assert res.status == "SKIPPED"


def test_aexecute_tool_error_surfaces_as_error():
    async def boom(tool_name, args):
        raise RuntimeError("apiserver refused")

    ex = Executor()
    res = asyncio.run(ex._aexecute_unchecked(FakeAction("restart"), "autonomous", dry_run=False, tool_caller=boom))
    assert res.status == "ERROR" and "apiserver refused" in res.detail


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": "REFUSED", "reason": "namespace denied"}, "REFUSED"),
        ('{"status":"ERROR","error":"kubectl failed"}', "ERROR"),
        ({"status": "OK", "applied": False}, "REFUSED"),
        ({"message": "accepted"}, "ERROR"),
    ],
)
def test_aexecute_propagates_structured_negative_outcomes(response, expected):
    async def caller(tool_name, args):
        return response

    result = asyncio.run(
        Executor()._aexecute_unchecked(
            FakeAction("restart"),
            "autonomous",
            dry_run=False,
            tool_caller=caller,
        )
    )
    assert result.status == expected


# ── Code-change remediation routing (github-exec backend) ───────────────────

def test_aexecute_revert_commit_routes_to_github_caller():
    calls = {}

    async def github_caller(tool_name, args):
        calls["tool"] = tool_name
        calls["args"] = args
        return {"status": "REVERT_REQUESTED", "applied": True, "tool": tool_name}

    async def infra_caller(tool_name, args):
        raise AssertionError("infra caller should not be used for a code change")

    ex = Executor()
    action = FakeAction("revert_commit", target="checkout-service", parameters={"commit_sha": "abc123"})
    res = asyncio.run(ex._aexecute_unchecked(action, "autonomous", dry_run=False,
                                             tool_caller=infra_caller, github_caller=github_caller))
    assert res.status == "EXECUTED"
    assert calls["tool"] == "create_revert_pr"
    assert calls["args"]["identifier"] == "abc123"


def test_aexecute_revert_commit_without_github_caller_errors():
    ex = Executor()
    action = FakeAction("revert_commit", parameters={"commit_sha": "abc123"})
    res = asyncio.run(ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=None, github_caller=None))
    assert res.status == "ERROR" and "github-exec" in res.detail


def test_infra_action_still_uses_infra_caller():
    async def infra_caller(tool_name, args):
        return {"status": "OK", "applied": True, "tool": tool_name}

    async def github_caller(tool_name, args):
        raise AssertionError("github caller should not be used for an infra action")

    ex = Executor()
    res = asyncio.run(ex._aexecute_unchecked(FakeAction("restart"), "autonomous", dry_run=False,
                                             tool_caller=infra_caller, github_caller=github_caller))
    assert res.status == "EXECUTED"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
