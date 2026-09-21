#!/usr/bin/env python3
"""A tool failure has to still be a failure by the time anyone reads it.

`wrap_all_tools_with_retry` composes layers — retry (inner), circuit breaker,
namespace scope, write guard, audit (outer) — and every layer above the first
is built to notice an *exception*. The retry wrapper returned a string
instead:

    return error.to_agent_response()

Probed on 2026-09-15 against a tool raising `ConnectionError` every attempt:

    returned type : str
    is_tool_error : False            # the module cannot parse its own output
    cb failures   : {}               # circuit breaker recorded nothing
    audit statuses: ['PENDING', 'SUCCESS']

So a total MCP outage was written to `AgentAuditLog` as SUCCESS, the circuit
breaker could never open, and — the part that reaches a human — langgraph's
`ToolNode` never set `ToolMessage.status == "error"`. `agent_nodes.py` calls
that status "the ONLY reliable signal for 'the tool itself failed'" and keys
`tool_failures` off it, so `agent_tool_failures` was structurally always empty
and the six sites in `supervisor.py` that caveat a conclusion with it never
fired. The system could not say "I concluded this with the metrics tool down."

A fifth defect fell out of the probe: `reraise=True` made tenacity re-raise the
original exception rather than `RetryError`, so the `except RetryError` branch
was dead. Every exhausted tool was reported as `retry_count=1`,
`is_recoverable=True`, and logged "failed on first attempt" — after three real
attempts. The audit trail's attempt count was wrong for every failure it ever
recorded.

These tests hold the failure to being a failure at each layer in turn.
"""

from __future__ import annotations

import asyncio
from operator import add
from typing import Annotated, TypedDict
from unittest.mock import patch

import pytest
import tenacity

from sre_agent import mcp_tool_wrapper as w
from sre_agent.mcp_tool_wrapper import (
    ToolError,
    ToolExecutionError,
    is_tool_error,
    parse_tool_error,
    wrap_all_tools_with_retry,
    wrap_tool_with_retry,
)


# ── fakes ────────────────────────────────────────────────────────────────────
class DeadTool:
    """A tool whose backing service is down."""

    def __init__(self, name: str = "prometheus_query", exc: Exception | None = None):
        self.name = name
        self.calls = 0
        self._exc = exc or ConnectionError("MCP prometheus unreachable")

    def invoke(self, *_a, **_k):
        self.calls += 1
        raise self._exc

    async def ainvoke(self, *_a, **_k):
        self.calls += 1
        raise self._exc


class FlakyTool:
    """Fails `fail_times`, then succeeds."""

    def __init__(self, fail_times: int, value=None, name: str = "loki_query"):
        self.name = name
        self.calls = 0
        self._fail_times = fail_times
        self._value = value if value is not None else {"series": [1]}

    def invoke(self, *_a, **_k):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("timeout")
        return self._value

    async def ainvoke(self, *_a, **_k):
        return self.invoke()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Collapse tenacity's sleep so the suite stays fast."""
    monkeypatch.setattr(w, "wait_exponential", lambda **_k: tenacity.wait_none())


@pytest.fixture(autouse=True)
def _reset_circuit():
    for bucket in w._CIRCUIT_BREAKER_STATE.values():
        bucket.clear()
    yield
    for bucket in w._CIRCUIT_BREAKER_STATE.values():
        bucket.clear()


@pytest.fixture
def audit():
    with patch.object(w, "log_audit_entry") as spy:
        spy.return_value = "audit-1"
        yield spy


def _statuses(audit_spy):
    return [call.args[1] for call in audit_spy.call_args_list]


# ---------------------------------------------------------------------------
# The retry layer: a spent tool raises
# ---------------------------------------------------------------------------

def test_an_exhausted_tool_raises_instead_of_returning_prose():
    tool = DeadTool()
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert tool.calls == 3
    assert caught.value.tool_error.tool_name == "prometheus_query"


def test_the_agent_still_gets_a_readable_explanation():
    """Raising must not cost the agent its graceful-degradation text — that
    string becomes the ToolMessage content langgraph hands back to the model."""
    wrapped = wrap_tool_with_retry(DeadTool(), max_attempts=2)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    text = str(caught.value)
    assert "prometheus_query" in text
    assert "Proceeding without this data" in text
    assert "MCP prometheus unreachable" in text


def test_the_reported_attempt_count_is_the_real_one():
    """The dead `except RetryError` branch reported 1 attempt after three."""
    tool = DeadTool()
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert tool.calls == 3
    assert caught.value.tool_error.retry_count == 3


def test_an_exhausted_tool_is_not_marked_recoverable():
    """`is_recoverable=True` told the reflector to try again against a service
    that had just refused three times."""
    wrapped = wrap_tool_with_retry(DeadTool(), max_attempts=3)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert caught.value.tool_error.is_recoverable is False


def test_a_transient_failure_still_retries_into_a_success():
    tool = FlakyTool(fail_times=2, value={"series": [{"metric": "up"}]})
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    assert wrapped.invoke({}) == {"series": [{"metric": "up"}]}
    assert tool.calls == 3


def test_a_working_tool_is_untouched():
    tool = FlakyTool(fail_times=0, value="ok")
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    assert wrapped.invoke({}) == "ok"
    assert tool.calls == 1


def test_the_async_path_raises_the_same_way():
    tool = DeadTool()
    wrapped = wrap_tool_with_retry(tool, max_attempts=2)

    with pytest.raises(ToolExecutionError) as caught:
        asyncio.run(wrapped.ainvoke({}))

    assert tool.calls == 2
    assert caught.value.tool_error.retry_count == 2


def test_the_original_exception_is_kept_as_the_cause():
    """A ConnectionError buried in a string is not debuggable."""
    wrapped = wrap_tool_with_retry(DeadTool(), max_attempts=2)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert isinstance(caught.value.__cause__, ConnectionError)


# ---------------------------------------------------------------------------
# The module's own detectors
# ---------------------------------------------------------------------------

def test_the_module_recognises_its_own_failure():
    """`is_tool_error` returned False for the wrapper's own output, so the
    reflector's "check for ToolError in findings" contract never fired."""
    wrapped = wrap_tool_with_retry(DeadTool(), max_attempts=1)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert is_tool_error(caught.value) is True
    parsed = parse_tool_error(caught.value)
    assert isinstance(parsed, ToolError)
    assert parsed.tool_name == "prometheus_query"


def test_a_plain_success_is_not_mistaken_for_an_error():
    assert is_tool_error({"series": [1]}) is False
    assert is_tool_error("everything is fine") is False
    assert parse_tool_error("everything is fine") is None


# ---------------------------------------------------------------------------
# The circuit breaker: reachable at last
# ---------------------------------------------------------------------------

def test_a_failing_tool_is_recorded_as_a_failure(audit):
    tool = DeadTool()
    wrapped = wrap_all_tools_with_retry([tool], max_attempts=2)[0]

    with pytest.raises(ToolExecutionError):
        wrapped.invoke({})

    assert w._CIRCUIT_BREAKER_STATE["failures"]["prometheus_query"] == 1


def test_a_dead_tool_eventually_opens_the_circuit(audit):
    """The point of the breaker: stop re-dialling a service that is down.
    Before the fix the counter never left zero, so it never opened and every
    call paid the full backoff forever."""
    tool = DeadTool()
    wrapped = wrap_all_tools_with_retry([tool], max_attempts=1)[0]

    for _ in range(w.CIRCUIT_BREAKER_THRESHOLD):
        with pytest.raises(ToolExecutionError):
            wrapped.invoke({})

    assert w._CIRCUIT_BREAKER_STATE["is_open"]["prometheus_query"] is True

    calls_before = tool.calls
    with pytest.raises(Exception, match="Circuit Breaker OPEN"):
        wrapped.invoke({})
    assert tool.calls == calls_before, "open circuit still reached the tool"


def test_a_recovered_tool_closes_the_circuit(audit):
    tool = FlakyTool(fail_times=1, value="ok")
    wrapped = wrap_all_tools_with_retry([tool], max_attempts=1)[0]

    with pytest.raises(ToolExecutionError):
        wrapped.invoke({})
    assert w._CIRCUIT_BREAKER_STATE["failures"]["loki_query"] == 1

    assert wrapped.invoke({}) == "ok"
    assert w._CIRCUIT_BREAKER_STATE["failures"]["loki_query"] == 0


# ---------------------------------------------------------------------------
# The audit trail: the provenance record must not claim success
# ---------------------------------------------------------------------------

def test_an_outage_is_audited_as_a_failure(audit):
    wrapped = wrap_all_tools_with_retry([DeadTool()], max_attempts=2)[0]

    with pytest.raises(ToolExecutionError):
        wrapped.invoke({})

    assert _statuses(audit) == ["PENDING", "FAILURE"]


def test_the_audited_failure_carries_the_reason(audit):
    wrapped = wrap_all_tools_with_retry([DeadTool()], max_attempts=2)[0]

    with pytest.raises(ToolExecutionError):
        wrapped.invoke({})

    final = audit.call_args_list[-1]
    assert "MCP prometheus unreachable" in str(final.kwargs.get("error"))


def test_a_real_success_is_still_audited_as_success(audit):
    wrapped = wrap_all_tools_with_retry([FlakyTool(fail_times=0, value="ok")], max_attempts=2)[0]

    assert wrapped.invoke({}) == "ok"
    assert _statuses(audit) == ["PENDING", "SUCCESS"]


def test_the_async_path_audits_a_failure_too(audit):
    wrapped = wrap_all_tools_with_retry([DeadTool()], max_attempts=2)[0]

    with pytest.raises(ToolExecutionError):
        asyncio.run(wrapped.ainvoke({}))

    assert _statuses(audit) == ["PENDING", "FAILURE"]


# ---------------------------------------------------------------------------
# The whole stack, which is the only thing that matters
# ---------------------------------------------------------------------------

def test_the_failure_survives_every_wrapper(audit):
    """Retry → circuit breaker → audit, composed as production composes them.
    One assertion per layer, because the bug was that each layer individually
    looked fine and the composition lost the signal."""
    tool = DeadTool()
    wrapped = wrap_all_tools_with_retry([tool], max_attempts=3)[0]

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert tool.calls == 3                                             # retry ran
    assert caught.value.tool_error.retry_count == 3                    # and said so
    assert w._CIRCUIT_BREAKER_STATE["failures"]["prometheus_query"]    # breaker saw it
    assert _statuses(audit) == ["PENDING", "FAILURE"]                  # audit saw it
    assert is_tool_error(caught.value)                                 # reflector can see it


# ---------------------------------------------------------------------------
# The join to langgraph, which is where the first version of this fix broke
# ---------------------------------------------------------------------------

class _GraphState(TypedDict):
    messages: Annotated[list, add]


def _run_through_tool_node(exc: Exception, *, handler):
    """Drive a real compiled graph, because a bare `ToolNode.invoke` has no
    runtime and the whole question here is what langgraph does with a raise."""
    from langchain_core.messages import AIMessage
    from langchain_core.tools import StructuredTool
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode

    def _boom(query: str) -> str:
        raise exc

    tool = StructuredTool.from_function(
        _boom, name="prometheus_query", description="query prometheus"
    )
    graph = StateGraph(_GraphState)
    graph.add_node("tools", ToolNode([tool], handle_tool_errors=handler))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)

    ai = AIMessage(
        content="",
        tool_calls=[{"name": "prometheus_query", "args": {"query": "up"}, "id": "tc1"}],
    )
    return graph.compile().invoke({"messages": [ai]})["messages"][-1]


def test_langgraphs_own_default_would_have_killed_the_investigation():
    """Why `handle_tool_execution_error` has to exist at all.

    The installed langgraph's default absorbs only `ToolInvocationError` and
    re-raises the rest, so raising without wiring a handler would have traded
    a silent wrong answer for a dead run.
    """
    from langgraph.prebuilt.tool_node import _default_handle_tool_errors

    with pytest.raises(ConnectionError):
        _run_through_tool_node(
            ConnectionError("MCP prometheus unreachable"),
            handler=_default_handle_tool_errors,
        )


def test_a_dead_tool_becomes_an_error_tool_message():
    """The signal `agent_nodes.py` keys `tool_failures` off — the assertion
    that ties the wrapper's contract to what a human reads in Slack."""
    message = _run_through_tool_node(
        ToolExecutionError(
            ToolError(
                tool_name="prometheus_query",
                error_message="MCP prometheus unreachable",
                retry_count=3,
            )
        ),
        handler=w.handle_tool_execution_error,
    )

    assert message.status == "error"
    assert "prometheus_query" in message.content
    assert "Proceeding without this data" in message.content


def test_an_unexpected_exception_still_crashes_loudly():
    """The handler is deliberately not a blanket catch. A bug in our own code
    must not be laundered into a sentence in the model's context — that is the
    same mistake this whole fix is undoing."""
    with pytest.raises(ZeroDivisionError):
        _run_through_tool_node(
            ZeroDivisionError("bug in the wrapper"),
            handler=w.handle_tool_execution_error,
        )


def test_the_specialist_records_the_failure_it_is_handed():
    """`agent_nodes` appends to `tool_failures` on `status == "error"` and on
    nothing else. Both directions, since an over-eager version of this would
    caveat every conclusion the platform ever reaches."""
    from langchain_core.messages import ToolMessage

    failed = ToolMessage(
        content="Error: Tool prometheus_query failed after 3 attempts.",
        name="prometheus_query",
        tool_call_id="tc1",
        status="error",
    )
    fine = ToolMessage(
        content="checkout-service returned 500 for 84% of requests",
        name="loki_query",
        tool_call_id="tc2",
    )

    assert getattr(failed, "status", "success") == "error"
    assert getattr(fine, "status", "success") == "success"


# ---------------------------------------------------------------------------
# The other half: a tool that RETURNS its failure
# ---------------------------------------------------------------------------
# MCP's own flag for this is `isError`, and the adapter raises `ToolException`
# on it. Our servers never set it -- they answer 200 with an error-shaped body
# -- so every consequence above came back through a door the first fix did not
# cover.


class PayloadFailingTool:
    """A server that is up, answers 200, and says the tool failed."""

    def __init__(self, value, name: str = "prometheus_query", fail_times: int = 99):
        self.name = name
        self.calls = 0
        self._value = value
        self._fail_times = fail_times

    def invoke(self, *_a, **_k):
        self.calls += 1
        if self.calls <= self._fail_times:
            return self._value
        return {"metric_names": ["up"]}

    async def ainvoke(self, *_a, **_k):
        return self.invoke()


def test_a_server_that_returns_its_failure_as_a_payload_still_raises():
    tool = PayloadFailingTool(
        {"metric_names": [], "error": "Could not connect to Prometheus"}
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    # The server's own words survive to the agent; only the framing changes.
    assert "Could not connect to Prometheus" in str(caught.value)
    assert caught.value.tool_error.retry_count == 3


def test_the_payload_failure_is_retried_because_that_is_what_it_is():
    """A connection error the server swallowed into a body is still a
    connection error. Retrying it is the whole point of this wrapper, and it
    was dead for every failure reported this way."""
    tool = PayloadFailingTool({"error": "Could not connect to Prometheus"}, fail_times=2)
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    assert wrapped.invoke({}) == {"metric_names": ["up"]}
    assert tool.calls == 3


def test_a_failure_wrapped_in_a_content_block_is_still_read():
    """Adapter results are `[{"type": "text", "text": "<json>"}]`, not the
    dict the server wrote. Reading only the dict shape would miss every real
    call."""
    tool = PayloadFailingTool(
        [{"type": "text", "text": '{"error": "Loki query failed: timeout"}'}],
        name="loki_query",
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=1)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert "Loki query failed: timeout" in str(caught.value)


def test_an_error_inside_the_data_is_data():
    """The counterweight. A log line about a 500, or a pod in phase Failed,
    is a working tool answering correctly -- raising there would open the
    breaker on a healthy server and blind the investigation to real evidence."""
    tool = PayloadFailingTool(
        {
            "status": "Failed",
            "reason": "OOMKilled",
            "series": [{"labels": {"error": "500"}, "values": [1]}],
        },
        name="loki_query",
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=1)

    assert wrapped.invoke({})["reason"] == "OOMKilled"
    assert tool.calls == 1


def test_the_payload_failure_reaches_the_breaker_and_the_audit_row(audit):
    """The point of raising: the layers above are built to notice an
    exception, and none of them could see this failure before."""
    tool = PayloadFailingTool({"error": "Could not connect to Prometheus"})
    wrapped = wrap_all_tools_with_retry([tool], max_attempts=2)[0]

    with pytest.raises(ToolExecutionError):
        wrapped.invoke({})

    assert _statuses(audit) == ["PENDING", "FAILURE"]
    assert w._CIRCUIT_BREAKER_STATE["failures"].get("prometheus_query") == 1


@pytest.mark.asyncio
async def test_the_async_path_reads_it_too():
    tool = PayloadFailingTool({"error": "No Notion credentials configured"},
                              name="get_runbook")
    wrapped = wrap_tool_with_retry(tool, max_attempts=1)

    with pytest.raises(ToolExecutionError) as caught:
        await wrapped.ainvoke({})

    assert "No Notion credentials configured" in str(caught.value)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
