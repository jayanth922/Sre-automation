#!/usr/bin/env python3
"""Unit tests for the dry-run Executor (ACT phase)."""

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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
