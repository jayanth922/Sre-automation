"""Activity-level retry classification for live remediation."""

import asyncio

import pytest
from temporalio.exceptions import ApplicationError

import sre_agent.act_phase as act_phase
import sre_agent.executor as executor
import sre_agent.incident_remediation_workflow as remediation_workflow
import sre_agent.multi_agent_langgraph as multi_agent_langgraph
from sre_agent.incident_remediation_workflow import (
    LiveRemediationInput,
    execute_live_action_activity,
)


PARAMS = LiveRemediationInput(
    incident_id="incident-1",
    organization_id="org-1",
    cluster_id="cluster-1",
)
REQUEST = {
    "action_index": 0,
    "action": {
        "action_type": "restart",
        "target": "checkout-service",
        "parameters": {"namespace": "demo-app"},
    },
}


def test_setup_failure_is_exposed_as_retryable_pre_dispatch(monkeypatch):
    async def fail_context(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        remediation_workflow, "_execution_context_for", fail_context
    )

    with pytest.raises(ApplicationError) as exc:
        asyncio.run(execute_live_action_activity(PARAMS, REQUEST))

    assert exc.value.type == "LiveActionPreDispatchError"


def test_cleanup_failure_does_not_replay_or_replace_success(monkeypatch):
    executions = []
    closes = []

    async def context(*args, **kwargs):
        return object()

    async def caller(*args, **kwargs):
        return None

    caller.mcp_client = object()

    async def build_caller(_context):
        return caller

    async def execute(request, tool_caller, **kwargs):
        executions.append(request["action_index"])
        return {
            "action_type": "restart",
            "target": "checkout-service",
            "status": "EXECUTED",
            "command": "restart checkout-service",
            "detail": "done",
        }

    async def fail_close(client):
        closes.append(client)
        raise RuntimeError("close failed")

    monkeypatch.setattr(remediation_workflow, "_execution_context_for", context)
    monkeypatch.setattr(executor, "build_executor_tool_caller", build_caller)
    monkeypatch.setattr(act_phase, "execute_live_action_request", execute)
    monkeypatch.setattr(multi_agent_langgraph, "close_mcp_client", fail_close)

    result = asyncio.run(execute_live_action_activity(PARAMS, REQUEST))

    assert result["status"] == "EXECUTED"
    assert executions == [0]
    assert closes == [caller.mcp_client]
