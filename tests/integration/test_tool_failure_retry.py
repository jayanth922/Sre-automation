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
def test_exhausted_retries_return_structured_tool_error(monkeypatch):
    tenacity = pytest.importorskip("tenacity")
    from sre_agent.mcp_tool_wrapper import wrap_tool_with_retry

    monkeypatch.setattr(
        "sre_agent.mcp_tool_wrapper.wait_exponential",
        lambda **_kwargs: tenacity.wait_none(),
    )

    tool = FakeMCPTool(
        name="loki_query",
        side_effects=[RuntimeError("down"), RuntimeError("down"), RuntimeError("down")],
    )
    wrapped = wrap_tool_with_retry(tool, max_attempts=3)
    result = wrapped.invoke({})
    # Wrapper returns an agent-facing error payload rather than raising.
    text = str(result).lower()
    assert "loki_query" in text or "unavailable" in text or "error" in text
    assert tool.calls >= 3
