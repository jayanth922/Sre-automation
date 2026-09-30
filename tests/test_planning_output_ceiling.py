"""Both planning calls get their own output ceiling.

The remediation plan used 2865-3300 of the 4096 chat default in all five
traced runs of 2026-09-30. A plan cut off there fails to parse and falls
back to escalation, so the planner is the next call to be cut off the way
every reflection of Runs 4-6 was.
"""

from __future__ import annotations

import asyncio
import inspect

from sre_agent import graph_builder, model_router, supervisor
from sre_agent.investigation_limits import investigation_limits


def test_the_planning_ceiling_is_operator_settable_and_bounded(monkeypatch):
    monkeypatch.delenv("PLANNING_MAX_OUTPUT_TOKENS", raising=False)
    assert investigation_limits().planning_max_output_tokens == 8192

    monkeypatch.setenv("PLANNING_MAX_OUTPUT_TOKENS", "10000")
    assert investigation_limits().planning_max_output_tokens == 10000

    monkeypatch.setenv("PLANNING_MAX_OUTPUT_TOKENS", "999999")
    assert investigation_limits().planning_max_output_tokens == 32000

    monkeypatch.setenv("PLANNING_MAX_OUTPUT_TOKENS", "1")
    assert investigation_limits().planning_max_output_tokens == 1024


def test_the_remediation_planner_asks_for_the_planning_ceiling(monkeypatch):
    seen = {}

    class LLM:
        def with_structured_output(self, schema, method=None, **kwargs):
            raise RuntimeError("stop after routing")

    def route(task_type, **kwargs):
        seen["task_type"] = task_type
        seen["max_tokens"] = kwargs.get("max_tokens")
        return LLM()

    monkeypatch.setattr(model_router, "route_llm", route)
    asyncio.run(graph_builder._planner_node({"metadata": {}}, []))

    assert seen["task_type"] == model_router.TaskType.PLANNING
    assert seen["max_tokens"] == investigation_limits().planning_max_output_tokens
    assert seen["max_tokens"] > 4096


def test_the_investigation_planner_asks_for_the_planning_ceiling():
    source = inspect.getsource(supervisor.SupervisorAgent.create_investigation_plan)
    routed = source[source.index("TaskType.PLANNING") :]
    call = routed[: routed.index("\n        )")]

    assert "max_tokens=investigation_limits().planning_max_output_tokens" in call
