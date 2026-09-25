#!/usr/bin/env python3
"""An investigating specialist must not be able to change the world.

`github_agent` was configured with `create_revert_pr`, `comment_on_pr` and
`revert_pr`. `github_exec/server.py` signs them `(identifier, dry_run = True)`
— a default the *model* may override — and `guardrail_check` validates the
repo and argument shape, never whether a human approved anything. So a
diagnosing agent could open a revert PR against the live repository while
still deciding what was wrong, and the whole approval apparatus (policy gate,
Slack `approve fix`, the executor's own MCP client) would never have been
consulted.

Two things had to be true for that to be closed, and this file asserts both:
the tools are gone from the specialist's list, and the guard refuses them
even if someone puts them back — a YAML list enforces nothing.

The refusal is audited as REFUSED, not FAILURE: "we did not let it run" is a
different event from "the tool broke", and an attempted unapproved write is
the one an operator most needs to find.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from sre_agent.investigation_write_guard import (
    ToolNotAuthorizedError,
    is_remediation_only,
    remediation_only_tools,
    wrap_tool_with_write_guard,
)

CONFIG = Path(__file__).resolve().parents[1] / "src" / "sre_agent" / "config" / "agent_config.yaml"


class FakeTool:
    """Minimal stand-in. `called` is the whole point: it must stay False."""

    def __init__(self, name):
        self.name = name
        self.called = False

    def invoke(self, args=None):
        self.called = True
        return {"ok": True}

    async def ainvoke(self, args=None):
        self.called = True
        return {"ok": True}


@pytest.fixture
def specialist_tools():
    config = yaml.safe_load(CONFIG.read_text())
    return {
        agent: set(spec.get("tools") or [])
        for agent, spec in config["agents"].items()
    }


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

def test_no_specialist_holds_a_remediation_tool(specialist_tools):
    forbidden = remediation_only_tools()
    offenders = {
        agent: sorted(tools & forbidden)
        for agent, tools in specialist_tools.items()
        if tools & forbidden
    }

    assert offenders == {}, (
        f"Investigating specialists were given remediation tools: {offenders}. "
        "These run only after a human approves, through executor.py's own "
        "client — never from the investigation graph."
    )


def test_the_github_specialist_kept_its_read_tools(specialist_tools):
    """Closing the write path must not blind the agent; correlating a deploy
    with an incident is its entire job."""
    assert {"list_commits", "get_commit", "list_pull_requests", "get_pull_request"} <= (
        specialist_tools["github_agent"]
    )


# ---------------------------------------------------------------------------
# The set itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool",
    [
        "create_revert_pr",
        "comment_on_pr",
        "restart_deployment",
        "scale_deployment",
        "rollback_deployment",
        "patch_resource_limits",
        "patch_deployment_env",
        "recreate_pod",
        "sandbox_provision",
        "sandbox_teardown",
    ],
)
def test_every_mutating_tool_is_covered(tool):
    assert is_remediation_only(tool)


@pytest.mark.parametrize(
    "tool",
    [
        "get_deployment_config",  # the "inspect" action — a read
        "sandbox_status",
        "sandbox_logs",
        "list_commits",
        "get_pod_logs",
        "get_metric",
        "search_runbooks",
    ],
)
def test_reads_are_never_forbidden(tool):
    """Nothing about looking is unsafe, and `policy_gate.decide` already lets
    read-only actions run unapproved. Forbidding them here would contradict
    the gate."""
    assert not is_remediation_only(tool)


def test_the_set_is_derived_from_the_executors_own_maps():
    """Hand-maintained denylists rot. A remediation tool added to executor.py
    tomorrow is covered the same day."""
    from sre_agent.executor import EXECUTOR_TOOL_MAP, GITHUB_EXEC_TOOL_MAP

    remediation_only_tools.cache_clear()
    with patch.dict(EXECUTOR_TOOL_MAP, {"nuke": "delete_everything"}), patch.dict(
        GITHUB_EXEC_TOOL_MAP, {"force_push": "force_push_main"}
    ):
        derived = remediation_only_tools()

    remediation_only_tools.cache_clear()
    assert "delete_everything" in derived
    assert "force_push_main" in derived


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

def test_a_forbidden_tool_is_never_called():
    tool = wrap_tool_with_write_guard(FakeTool("create_revert_pr"))

    with pytest.raises(ToolNotAuthorizedError):
        tool.invoke({"identifier": "abc123", "dry_run": False})

    assert tool.called is False


@pytest.mark.asyncio
async def test_the_async_path_refuses_too():
    """The specialists call tools through `ainvoke`; a guard only on the sync
    path would guard nothing."""
    tool = wrap_tool_with_write_guard(FakeTool("restart_deployment"))

    with pytest.raises(ToolNotAuthorizedError):
        await tool.ainvoke({"deployment": "checkout-service"})

    assert tool.called is False


def test_dry_run_true_is_refused_as_well():
    """`dry_run=True` is the model's claim about its own call, and the model
    is what is being constrained. It can flip that argument as easily as it
    set it."""
    tool = wrap_tool_with_write_guard(FakeTool("create_revert_pr"))

    with pytest.raises(ToolNotAuthorizedError):
        tool.invoke({"identifier": "abc123", "dry_run": True})


def test_the_refusal_tells_the_agent_what_to_do_instead():
    tool = wrap_tool_with_write_guard(FakeTool("create_revert_pr"))

    with pytest.raises(ToolNotAuthorizedError) as caught:
        tool.invoke({})

    message = str(caught.value)
    assert "create_revert_pr" in message
    assert "Nothing was called" in message
    assert "approves" in message


def test_a_read_tool_passes_straight_through():
    tool = wrap_tool_with_write_guard(FakeTool("list_commits"))

    assert tool.invoke({}) == {"ok": True}
    assert tool.called is True


# ---------------------------------------------------------------------------
# What the audit log records
# ---------------------------------------------------------------------------

def test_a_refusal_is_audited_as_refused_not_failure():
    from sre_agent.mcp_tool_wrapper import wrap_tool_with_audit

    statuses = []
    guarded = wrap_tool_with_write_guard(FakeTool("create_revert_pr"))

    with patch(
        "sre_agent.mcp_tool_wrapper.log_audit_entry",
        side_effect=lambda name, status, *a, **kw: statuses.append(status),
    ):
        audited = wrap_tool_with_audit(guarded)
        with pytest.raises(ToolNotAuthorizedError):
            audited.invoke({})

    assert statuses == ["PENDING", "REFUSED"]


def test_a_real_tool_failure_is_still_audited_as_failure():
    """The new status must not swallow the old one."""
    from sre_agent.mcp_tool_wrapper import wrap_tool_with_audit

    statuses = []
    tool = FakeTool("get_metric")
    tool.invoke = lambda args=None: (_ for _ in ()).throw(ConnectionError("down"))

    with patch(
        "sre_agent.mcp_tool_wrapper.log_audit_entry",
        side_effect=lambda name, status, *a, **kw: statuses.append(status),
    ):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(ConnectionError):
            audited.invoke({})

    assert statuses == ["PENDING", "FAILURE"]


def test_the_guard_sits_inside_audit_in_the_real_stack():
    """Composition, not a unit: `wrap_all_tools_with_retry` must place the
    guard where the refusal is recorded and nothing retries it."""
    from sre_agent import mcp_tool_wrapper

    statuses = []
    tool = FakeTool("create_revert_pr")

    with patch.object(
        mcp_tool_wrapper,
        "log_audit_entry",
        side_effect=lambda name, status, *a, **kw: statuses.append(status),
    ):
        wrapped = mcp_tool_wrapper.wrap_all_tools_with_retry([tool], max_attempts=3)
        with pytest.raises(ToolNotAuthorizedError):
            wrapped[0].invoke({})

    assert tool.called is False
    assert statuses == ["PENDING", "REFUSED"]
    # One attempt, not three: there is nothing transient about a refusal.
    assert statuses.count("PENDING") == 1


def test_a_refusal_does_not_trip_the_circuit_breaker():
    """The tool was never contacted, so it is not unhealthy. Opening its
    breaker would take a working tool offline for the approved path."""
    from sre_agent import mcp_tool_wrapper

    mcp_tool_wrapper._CIRCUIT_BREAKER_STATE["failures"].pop("create_revert_pr", None)
    mcp_tool_wrapper._CIRCUIT_BREAKER_STATE["is_open"].pop("create_revert_pr", None)

    with patch.object(mcp_tool_wrapper, "log_audit_entry", return_value=None):
        wrapped = mcp_tool_wrapper.wrap_all_tools_with_retry(
            [FakeTool("create_revert_pr")], max_attempts=3
        )
        for _ in range(mcp_tool_wrapper.CIRCUIT_BREAKER_THRESHOLD + 1):
            with pytest.raises(ToolNotAuthorizedError):
                wrapped[0].invoke({})

    assert not mcp_tool_wrapper._CIRCUIT_BREAKER_STATE["is_open"].get("create_revert_pr")


# ---------------------------------------------------------------------------
# The approved path must still work
# ---------------------------------------------------------------------------

def test_the_executor_does_not_go_through_the_guard():
    """The guard wraps tools from `wrap_all_tools_with_retry`, which only the
    investigation graph calls (`multi_agent_langgraph.py`). The executor
    builds its own client in `build_mcp_tool_caller`, so approved remediation
    never meets this code. If that ever changes, this test is where it
    surfaces — remediation would start refusing itself."""
    import inspect

    from sre_agent import executor

    source = inspect.getsource(executor)
    assert "wrap_all_tools_with_retry" not in source
    assert "MultiServerMCPClient" in source


def test_github_exec_still_maps_the_approved_actions():
    """Removing the tools from the specialist must not remove the capability.
    A revert the human approves still has something to run it."""
    from sre_agent.executor import GITHUB_EXEC_TOOL_MAP

    assert GITHUB_EXEC_TOOL_MAP["revert_commit"] == "create_revert_pr"
    assert GITHUB_EXEC_TOOL_MAP["comment_pr"] == "comment_on_pr"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
