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
    _live_args,
    build_command,
    build_rollback_command,
    classify_live_response,
    live_tool_for_action,
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


def test_build_command_recreate_pod():
    cmd = build_command(FakeAction("recreate_pod", target="checkout-service-7d9f-x2k4p",
                                    parameters={"namespace": "demo-app"}))
    assert cmd == "kubectl delete pod/checkout-service-7d9f-x2k4p -n demo-app"


def test_rollback_command_for_recreate_pod_is_none():
    # The controller already recreated the pod; there is nothing to "undo".
    assert build_rollback_command(FakeAction("recreate_pod")) is None


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


def test_aexecute_live_recreate_pod_routes_to_mapped_tool():
    calls = {}

    async def fake_caller(tool_name, args):
        calls["tool"] = tool_name
        calls["args"] = args
        return {"status": "OK", "tool": tool_name}

    ex = Executor()
    action = FakeAction("recreate_pod", target="checkout-service-7d9f-x2k4p",
                         parameters={"namespace": "demo-app"})
    res = asyncio.run(ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=fake_caller))
    assert res.status == "EXECUTED"
    assert calls["tool"] == "recreate_pod"
    assert calls["args"]["name"] == "checkout-service-7d9f-x2k4p"


# ── _live_args target parsing ───────────────────────────────────────────────

def test_live_args_bare_name_passthrough():
    args = _live_args(FakeAction("restart", target="checkout-service"))
    assert args["name"] == "checkout-service"


def test_live_args_strips_colon_subresource_suffix():
    args = _live_args(FakeAction("restart", target="checkout-service:process-handler-pods"))
    assert args["name"] == "checkout-service"


def test_live_args_extracts_name_from_descriptive_phrase():
    target = "checkout-service pods (targeted canary subset only, e.g. one pod from replicaset 859599c74b)"
    args = _live_args(FakeAction("restart", target=target))
    assert args["name"] == "checkout-service"


def test_live_args_uppercase_target_is_lowercased():
    args = _live_args(FakeAction("restart", target="Checkout-Service"))
    assert args["name"] == "checkout-service"


# ── classify_live_response: real MCP content-block shape ───────────────────
# The MCP SDK wraps a tool's JSON payload as a list of plain dicts like
# {"type": "text", "text": "<json>", "id": ...} — captured verbatim from a
# live restart_deployment call that actually succeeded at the cluster level
# but was misclassified as ERROR before this fix (the parser only recursed
# into "result"/"data"/"content" keys, never "text").

def test_classify_live_response_unwraps_mcp_text_content_block_success():
    response = [
        {
            "type": "text",
            "text": (
                '{"tool":"restart","name":"checkout-service","namespace":"meridian",'
                '"dry_run":false,"applied":true,'
                '"kubectl_equivalent":"kubectl rollout restart deployment/checkout-service -n meridian",'
                '"status":"OK","restartedAt":"2026-09-01T21:54:32.426055+00:00"}'
            ),
            "id": "lc_e8f5309e-5ea5-435b-81f1-e950933fe481",
        }
    ]
    status, detail = classify_live_response(response)
    assert status == "EXECUTED"
    assert "checkout-service" in detail


def test_classify_live_response_unwraps_mcp_text_content_block_refusal():
    response = [
        {
            "type": "text",
            "text": '{"tool":"patch_resource_limits","namespace":"meridian","status":"REFUSED","reason":"provide at least one of memory/cpu"}',
            "id": "lc_86f291ee-c27f-43e5-9b2b-5b91a6ba497e",
        }
    ]
    status, detail = classify_live_response(response)
    assert status == "REFUSED"


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


# --- Capability routing -------------------------------------------------
# `patch`/`config_change` map to patch_resource_limits, which changes nothing
# but cpu/memory limits. Routing on the action's *name* made the platform claim
# it could apply any config change, then refuse at the MCP server with "provide
# at least one of memory/cpu" — blaming the planner for a missing capability.


def test_config_change_with_a_memory_limit_is_executable():
    action = FakeAction("config_change", parameters={"namespace": "meridian", "memory": "512Mi"})
    assert live_tool_for_action(action) == "patch_resource_limits"


def test_config_change_with_a_nested_cpu_limit_is_executable():
    action = FakeAction(
        "config_change",
        parameters={"namespace": "meridian", "resources": {"limits": {"cpu": "500m"}}},
    )
    assert live_tool_for_action(action) == "patch_resource_limits"


def test_env_var_config_change_has_no_live_tool():
    # The shape the planner actually emits for a runtime toggle: prose intent,
    # no cpu/memory anywhere. There is no executor tool that can do this.
    action = FakeAction(
        "config_change",
        target="deployment/inventory-service",
        parameters={
            "namespace": "meridian",
            "intent": "Set SLOW_QUERY_RATE back to 0 via /admin/config and mirror it into the deployment env.",
            "blast_radius": "inventory-service pods only",
        },
    )
    assert live_tool_for_action(action) is None


def test_inspect_only_config_change_has_no_live_tool():
    action = FakeAction(
        "config_change",
        parameters={"namespace": "meridian", "mode": "inspect_only", "intent": "dump /admin/config"},
    )
    assert live_tool_for_action(action) is None


def test_patch_without_resource_limits_has_no_live_tool():
    action = FakeAction("patch", parameters={"namespace": "meridian", "patch": {"spec": {"paused": True}}})
    assert live_tool_for_action(action) is None


def test_actions_backed_by_their_own_tool_are_unaffected():
    assert live_tool_for_action(FakeAction("restart")) == "restart_deployment"
    assert live_tool_for_action(FakeAction("scale", parameters={"replicas": 3})) == "scale_deployment"
    assert live_tool_for_action(FakeAction("rollback")) == "rollback_deployment"
    assert live_tool_for_action(FakeAction("recreate_pod")) == "recreate_pod"
    # Notify-only and code-change actions are other dispatch families entirely.
    assert live_tool_for_action(FakeAction("escalate")) is None
    assert live_tool_for_action(FakeAction("revert_commit")) is None


def test_uncapable_config_change_is_refused_by_name_not_sent_to_the_tool():
    async def fail_caller(tool_name, args):
        raise AssertionError(f"no tool should be called; got {tool_name}({args})")

    ex = Executor()
    action = FakeAction("config_change", parameters={"namespace": "meridian", "intent": "flip a feature flag"})
    res = asyncio.run(
        ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=fail_caller)
    )
    assert res.status == "REFUSED"
    assert "no automation capability" in res.detail
    # The old failure blamed a missing parameter for a missing capability.
    assert "provide at least one of memory/cpu" not in res.detail


def test_capable_config_change_still_reaches_the_resource_tool():
    seen: Dict[str, Any] = {}

    async def caller(tool_name, args):
        seen["tool"], seen["args"] = tool_name, args
        return {"status": "OK", "applied": True}

    ex = Executor()
    action = FakeAction("config_change", parameters={"namespace": "meridian", "memory": "1Gi"})
    res = asyncio.run(
        ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=caller)
    )
    assert res.status == "EXECUTED"
    assert seen["tool"] == "patch_resource_limits"
    assert seen["args"]["memory"] == "1Gi"


# --- Env-var config changes ---------------------------------------------
# The second executable configuration surface. A runtime toggle (feature flag,
# fault-injection rate, log level) is the config change the planner actually
# proposes most often, and before patch_deployment_env existed it was the
# capability gap the routing above had to report.


def test_env_config_change_routes_to_the_env_tool():
    action = FakeAction(
        "config_change",
        parameters={"namespace": "meridian", "env": {"SLOW_QUERY_RATE": "0"}},
    )
    assert live_tool_for_action(action) == "patch_deployment_env"


def test_nested_and_aliased_env_maps_are_found():
    for params in (
        {"env_vars": {"LOG_LEVEL": "debug"}},
        {"spec": {"environment_variables": {"LOG_LEVEL": "debug"}}},
        {"changes": [{"env": {"LOG_LEVEL": "debug"}}]},
    ):
        assert live_tool_for_action(FakeAction("config_change", parameters=params)) == (
            "patch_deployment_env"
        ), params


def test_an_environment_name_is_not_an_env_map():
    # "environment: production" is the deployment's environment, not a variable
    # to set. Treating it as one would patch a container with ENVIRONMENT=... .
    action = FakeAction("config_change", parameters={"environment": "production"})
    assert live_tool_for_action(action) is None


def test_a_resource_limit_wins_over_env_when_both_are_present():
    # One action, one tool. The resource tool is the narrower, better-understood
    # mutation, and _live_args deliberately omits env when limits are present.
    action = FakeAction(
        "config_change",
        parameters={"memory": "512Mi", "env": {"LOG_LEVEL": "debug"}},
    )
    assert live_tool_for_action(action) == "patch_resource_limits"
    assert "env" not in _live_args(action)


def test_env_config_change_reaches_the_env_tool_with_container_and_values():
    seen: Dict[str, Any] = {}

    async def caller(tool_name, args):
        seen["tool"], seen["args"] = tool_name, args
        return {"status": "OK", "applied": True}

    ex = Executor()
    action = FakeAction(
        "config_change",
        target="inventory-service",
        parameters={
            "namespace": "meridian",
            "container": "inventory-service",
            "env": {"SLOW_QUERY_RATE": "0"},
        },
    )
    res = asyncio.run(
        ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=caller)
    )
    assert res.status == "EXECUTED"
    assert seen["tool"] == "patch_deployment_env"
    assert seen["args"]["env"] == {"SLOW_QUERY_RATE": "0"}
    assert seen["args"]["container"] == "inventory-service"
    assert seen["args"]["namespace"] == "meridian"


def test_env_build_command_is_the_kubectl_a_human_would_run():
    cmd = build_command(
        FakeAction(
            "config_change",
            target="inventory-service",
            parameters={
                "namespace": "meridian",
                "env": {"SLOW_QUERY_RATE": "0"},
            },
        )
    )
    assert "kubectl set env deployment/inventory-service" in cmd
    assert "SLOW_QUERY_RATE=0" in cmd
    assert "-n meridian" in cmd


def test_a_credential_named_env_value_never_reaches_the_transcript():
    # The edge refuses the write, but the rendered command is persisted in the
    # audit trail and echoed to Slack — the refusal must not be what logs it.
    cmd = build_command(
        FakeAction(
            "config_change",
            parameters={"env": {"DATABASE_PASSWORD": "hunter2", "LOG_LEVEL": "debug"}},
        )
    )
    assert "hunter2" not in cmd
    assert "DATABASE_PASSWORD=[REDACTED]" in cmd
    # A genuine flag is still legible: redaction keys off the name, not the value.
    assert "LOG_LEVEL=debug" in cmd


def test_unexecutable_config_change_does_not_print_a_command_nothing_runs():
    cmd = build_command(
        FakeAction("config_change", parameters={"intent": "re-apply the ConfigMap"})
    )
    assert "kubectl apply -f" not in cmd
    assert "no executable config change" in cmd


# --- Read-only inspection ------------------------------------------------


def test_inspect_routes_to_the_read_only_tool():
    assert live_tool_for_action(FakeAction("inspect")) == "get_deployment_config"


def test_inspect_args_carry_no_dry_run():
    # There is nothing to not-do on a read; the tool takes no such parameter.
    args = _live_args(FakeAction("inspect", parameters={"namespace": "meridian"}))
    assert "dry_run" not in args
    assert args["namespace"] == "meridian"


def test_inspect_command_is_a_read():
    cmd = build_command(FakeAction("inspect", parameters={"namespace": "meridian"}))
    assert cmd.startswith("kubectl get deployment/checkout-service")
    assert "read-only" in cmd


def test_inspect_reaches_the_config_dump_tool():
    seen: Dict[str, Any] = {}

    async def caller(tool_name, args):
        seen["tool"], seen["args"] = tool_name, args
        return {"status": "OK", "mutated": False, "replicas": 2}

    ex = Executor()
    action = FakeAction(
        "inspect", target="inventory-service", parameters={"namespace": "meridian"}
    )
    res = asyncio.run(
        ex._aexecute_unchecked(action, "autonomous", dry_run=False, tool_caller=caller)
    )
    assert res.status == "EXECUTED"
    assert seen["tool"] == "get_deployment_config"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
