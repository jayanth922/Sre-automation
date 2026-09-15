"""Tool failure + retry via MCP wrapper and deterministic fake tools."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LLM_PROVIDER", "anthropic")

from tests.integration.conftest import FakeMCPTool


@pytest.mark.integration
def test_transient_tool_failure_retries_then_succeeds(monkeypatch):
    tenacity = pytest.importorskip("tenacity")
    from sre_agent.mcp_tool_wrapper import wrap_tool_with_retry

    # Collapse backoff so the integration suite stays fast.
    monkeypatch.setattr(
        "sre_agent.mcp_tool_wrapper.wait_exponential",
        lambda **_kwargs: tenacity.wait_none(),
    )

    tool = FakeMCPTool(
        name="prometheus_query",
        side_effects=[
            RuntimeError("timeout"),
            RuntimeError("timeout"),
            {"series": [{"metric": "up", "value": 1}]},
        ],
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)
    result = wrapped.invoke({})
    assert result == {"series": [{"metric": "up", "value": 1}]}
    assert tool.calls == 3


@pytest.mark.integration
def test_exhausted_retries_raise_a_structured_tool_error(monkeypatch):
    """This test used to assert the defect.

    It read "Wrapper returns an agent-facing error payload rather than
    raising" and passed on a plain string — which is precisely how a dead MCP
    server reached `AgentAuditLog` as SUCCESS and left `agent_tool_failures`
    empty. See `tests/test_tool_failure_contract.py` for the full account.
    """
    tenacity = pytest.importorskip("tenacity")
    from sre_agent.mcp_tool_wrapper import ToolExecutionError, wrap_tool_with_retry

    monkeypatch.setattr(
        "sre_agent.mcp_tool_wrapper.wait_exponential",
        lambda **_kwargs: tenacity.wait_none(),
    )

    tool = FakeMCPTool(
        name="loki_query",
        side_effects=[RuntimeError("down"), RuntimeError("down"), RuntimeError("down")],
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)

    with pytest.raises(ToolExecutionError) as caught:
        wrapped.invoke({})

    assert tool.calls >= 3
    assert caught.value.tool_error.tool_name == "loki_query"
    assert caught.value.tool_error.retry_count == 3
    assert "loki_query" in str(caught.value)
