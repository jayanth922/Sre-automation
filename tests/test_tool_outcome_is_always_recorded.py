#!/usr/bin/env python3
"""Every tool call ends in a terminal audit row, and a refusal is not a crash.

Two live observations on 2026-09-15, from `agent_audit_logs` on the running
platform:

* 18 rows sat at PENDING forever — 2,269 others had a terminal status. They
  always came in same-millisecond bursts, and in incident f643ed9e the burst
  sits immediately after a `list_namespaces` FAILURE while *later* tools in
  the same investigation completed normally. The process did not die; the
  calls were cancelled, and `asyncio.CancelledError` is a `BaseException`,
  so `except Exception` in the audit wrapper never saw them. To the
  dashboard and to the audit export those calls are still executing.

* All 8 FAILURE rows in the whole table came from one source: the
  tenant-scope refusal of `list_namespaces`. Probed against a compiled
  one-node graph, that refusal did not just fail its own call — it escaped
  the node and took every sibling tool call in the same parallel batch with
  it. That is where the orphaned PENDING rows came from, and the same would
  have been true of the new write guard's refusal, whose message politely
  asks the agent to carry on with read-only tools it had just killed.

So: policy refusals are absorbed into a `ToolMessage` the model can read,
and every exception — `BaseException` included — closes its audit row.
An unexpected exception still crashes the node. That line is deliberate:
a refusal is a decision this system made, a `RuntimeError` is a bug, and
only one of those should be quietly turned into a sentence.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from sre_agent.investigation_write_guard import ToolNotAuthorizedError
from sre_agent.mcp_tool_wrapper import (
    ToolError,
    ToolExecutionError,
    _audit_error_text,
    _audit_status_for,
    handle_tool_execution_error,
    policy_refusals,
    wrap_tool_with_audit,
)
from sre_agent.namespace_scope import InvestigationQueryScopeError, NamespaceScopeError


def _exhausted(tool="get_metric"):
    return ToolExecutionError(
        ToolError(
            tool_name=tool,
            error_message="prometheus unreachable",
            retry_count=3,
            is_recoverable=False,
            suggestion="Proceed with data from other tools.",
        )
    )


# ---------------------------------------------------------------------------
# What counts as a refusal
# ---------------------------------------------------------------------------

def test_all_refusal_types_are_named():
    assert set(policy_refusals()) == {
        ToolNotAuthorizedError,
        NamespaceScopeError,
        InvestigationQueryScopeError,
    }


def test_a_bare_permission_error_is_not_a_refusal():
    """Matching on `PermissionError` would sweep in a real 403 from a cluster
    we genuinely lack RBAC for. That is an environment failure the on-call
    needs to see as a failure, not as this platform's own policy."""
    assert _audit_status_for(PermissionError("403 from the apiserver")) == "FAILURE"


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ToolNotAuthorizedError("create_revert_pr"), "REFUSED"),
        (NamespaceScopeError("cluster-wide listing is unavailable"), "REFUSED"),
        (InvestigationQueryScopeError("query window is too broad"), "REFUSED"),
        (asyncio.CancelledError(), "CANCELLED"),
        (ConnectionError("prometheus down"), "FAILURE"),
        (_exhausted(), "FAILURE"),
    ],
)
def test_the_audit_status_distinguishes_all_three_outcomes(exc, expected):
    assert _audit_status_for(exc) == expected


def test_a_cancelled_call_still_says_why():
    """`str(CancelledError())` is the empty string, and a blank error column
    explains nothing to whoever reads the row later."""
    text = _audit_error_text(asyncio.CancelledError())

    assert text
    assert "parallel" in text


def test_an_exception_with_a_message_keeps_its_own_words():
    assert _audit_error_text(ConnectionError("prometheus down")) == "prometheus down"


# ---------------------------------------------------------------------------
# The PENDING row always gets closed
# ---------------------------------------------------------------------------

class FakeTool:
    def __init__(self, name, raises=None):
        self.name = name
        self.raises = raises
        self.called = False

    def invoke(self, args=None):
        self.called = True
        if self.raises:
            raise self.raises
        return {"ok": True}

    async def ainvoke(self, args=None):
        self.called = True
        if self.raises:
            raise self.raises
        return {"ok": True}


def test_a_cancelled_async_call_closes_its_row_instead_of_staying_pending():
    rows = []

    def record(name, status, *a, **kw):
        rows.append(status)
        return "audit-id"

    tool = FakeTool("list_pods", raises=asyncio.CancelledError())
    with patch("sre_agent.mcp_tool_wrapper.log_audit_entry", side_effect=record):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(audited.ainvoke({}))

    assert rows == ["PENDING", "CANCELLED"], rows


def test_a_cancelled_sync_call_closes_its_row_too():
    rows = []
    tool = FakeTool("list_pods", raises=asyncio.CancelledError())

    with patch(
        "sre_agent.mcp_tool_wrapper.log_audit_entry",
        side_effect=lambda name, status, *a, **kw: rows.append(status),
    ):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(asyncio.CancelledError):
            audited.invoke({})

    assert rows == ["PENDING", "CANCELLED"], rows


def test_the_cancellation_is_re_raised_not_swallowed():
    """Swallowing a `CancelledError` breaks cooperative cancellation: the
    task that asked to stop would keep running."""
    tool = FakeTool("list_pods", raises=asyncio.CancelledError())

    with patch("sre_agent.mcp_tool_wrapper.log_audit_entry", return_value=None):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(audited.ainvoke({}))


def test_a_scope_refusal_is_recorded_as_refused_not_failure():
    """All 8 FAILURE rows in the live table were this. None of them was a
    tool that broke."""
    rows = []
    tool = FakeTool(
        "list_namespaces",
        raises=NamespaceScopeError("cluster-wide listing is unavailable"),
    )

    with patch(
        "sre_agent.mcp_tool_wrapper.log_audit_entry",
        side_effect=lambda name, status, *a, **kw: rows.append(status),
    ):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(NamespaceScopeError):
            audited.invoke({})

    assert rows == ["PENDING", "REFUSED"], rows


def test_an_ordinary_failure_is_still_a_failure():
    rows = []
    tool = FakeTool("get_metric", raises=ConnectionError("prometheus down"))

    with patch(
        "sre_agent.mcp_tool_wrapper.log_audit_entry",
        side_effect=lambda name, status, *a, **kw: rows.append(status),
    ):
        audited = wrap_tool_with_audit(tool)
        with pytest.raises(ConnectionError):
            audited.invoke({})

    assert rows == ["PENDING", "FAILURE"], rows


def test_the_live_terminal_has_a_line_for_an_abandoned_call():
    """Otherwise the dashboard's last word on the call is the 🔧 EXECUTING
    line, standing hours after it was abandoned."""
    import sre_agent.mcp_tool_wrapper as wrapper

    lines = []

    class FakeStore:
        def append_log(self, incident_id, msg):
            lines.append(msg)

    with patch.object(
        wrapper,
        "get_audit_context",
        return_value=("inc-1", "k8s_agent", None, None, None),
    ), patch("sre_agent.redis_state_store.get_state_store", return_value=FakeStore()), patch.object(
        wrapper, "SessionLocal", side_effect=RuntimeError("no database in this test")
    ):
        wrapper.log_audit_entry(
            "list_pods", "CANCELLED", {}, error="Cancelled before it returned"
        )

    assert any("CANCELLED: list_pods" in line for line in lines), lines


# ---------------------------------------------------------------------------
# A refusal must not take its siblings down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        ToolNotAuthorizedError("create_revert_pr"),
        NamespaceScopeError("cluster-wide listing is unavailable"),
        InvestigationQueryScopeError("query window is too broad"),
        _exhausted(),
    ],
)
def test_the_handler_absorbs_what_the_model_should_read(exc):
    message = handle_tool_execution_error(exc)

    assert isinstance(message, str) and message


def test_the_handler_still_re_raises_a_genuine_bug():
    """The blanket catch stays off. A `KeyError` in our own code is not a
    sentence to drop into the model's context."""
    with pytest.raises(KeyError):
        handle_tool_execution_error(KeyError("service_name"))


def _batch_graph(first_exc):
    """One node, two tool calls, exactly the shape `create_react_agent`
    builds — a bare ToolNode has no langgraph runtime and cannot be invoked
    on its own."""
    from langchain_core.messages import AIMessage
    from langchain_core.tools import StructuredTool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    finished = []

    async def bad_tool():
        raise first_exc

    async def list_pods():
        await asyncio.sleep(0.05)
        finished.append("list_pods")
        return json.dumps({"count": 3})

    node = ToolNode(
        [
            StructuredTool.from_function(coroutine=bad_tool, name="bad_tool", description="x"),
            StructuredTool.from_function(coroutine=list_pods, name="list_pods", description="y"),
        ],
        handle_tool_errors=handle_tool_execution_error,
    )
    builder = StateGraph(MessagesState)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)

    call = AIMessage(
        content="",
        tool_calls=[
            {"name": "bad_tool", "args": {}, "id": "call_bad"},
            {"name": "list_pods", "args": {}, "id": "call_pods"},
        ],
    )
    return builder.compile(), call, finished


@pytest.mark.parametrize(
    "exc",
    [
        ToolNotAuthorizedError("create_revert_pr"),
        NamespaceScopeError("cluster-wide listing is unavailable"),
        InvestigationQueryScopeError("query window is too broad"),
    ],
)
def test_a_refusal_leaves_the_sibling_read_alone(exc):
    """This is the defect, not a detail of it: the refusal text tells the
    agent to diagnose with read-only tools, and before this it killed the
    read-only tools it was telling the agent to use."""
    app, call, finished = _batch_graph(exc)

    out = asyncio.run(app.ainvoke({"messages": [call]}))

    assert finished == ["list_pods"], "the sibling call was cancelled"
    statuses = {
        message.name: getattr(message, "status", None)
        for message in out["messages"]
        if getattr(message, "name", None)
    }
    assert statuses["bad_tool"] == "error"
    assert statuses["list_pods"] == "success"


def test_an_unexpected_exception_still_stops_the_node():
    """Documented on purpose. A bug should be loud, and the sibling's own
    audit row now records CANCELLED rather than claiming to still be
    running."""
    app, call, finished = _batch_graph(RuntimeError("a genuine bug"))

    with pytest.raises(RuntimeError):
        asyncio.run(app.ainvoke({"messages": [call]}))

    assert finished == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
