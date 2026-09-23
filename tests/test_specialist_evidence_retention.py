#!/usr/bin/env python3
"""A lane that runs out of budget must report its evidence, not lose it.

The graded ``inventory_slow_queries`` trial told the supervisor "we have no
application logs". Loki had in fact answered four times, in about 0.1s each —
the 120s deadline was spent entirely inside model calls (p90 26.1s, max
68.6s), and the ``asyncio.TimeoutError`` handler *replaced* ``agent_response``
with a one-line "timed out" string. Since ``agent_results[agent_key]`` is
derived from that variable alone, every tool result the lane had already paid
for was discarded, and the lane then bought a narration model call to
paraphrase the loss.

Three boundaries are held here:

* the hard deadline keeps a digest of what was collected;
* a soft deadline declines to *start* a turn the clock cannot finish;
* a cut-short lane skips narration, the one model call with no reader.

Plus the output ceiling that made those turns long in the first place: the
live LiteLLM transport silently dropped ``default_max_tokens``, which the
provider path applies, so no specialist call has ever had one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from sre_agent import agent_nodes
from sre_agent.agent_nodes import (
    _REACT_STEPS_PER_TURN,
    SpecialistTurnBudget,
    _create_llm,
    _cut_short_reason,
    _partial_evidence_digest,
    _specialist_recursion_limit,
    _turn_headroom_seconds,
)
from sre_agent.constants import SREConstants
from sre_agent.investigation_limits import investigation_limits
from sre_agent.narrative import build_specialist_task_brief

RUNBOOK = """
## Remediation

For InventorySlowQueries use the database histogram instead:

```
histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))
```
"""


def _tool_message(name, content, *, status="success"):
    return SimpleNamespace(
        tool_call_id=f"call-{name}", name=name, content=content, status=status
    )


def _ai_message(content="thinking", *, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _step(*, tool_call: bool):
    return {"messages": [_ai_message(tool_calls=[{"name": "q"}] if tool_call else [])]}


# --- the hard deadline keeps what it already has ------------------------------


def test_the_digest_carries_every_tool_result_the_lane_collected():
    digest = _partial_evidence_digest(
        [
            _ai_message(),
            _tool_message("query_logs", "connection pool exhausted x412"),
            _tool_message("analyze_log_patterns", "top pattern: db_pool_exhausted"),
        ]
    )

    assert "query_logs" in digest
    assert "connection pool exhausted x412" in digest
    assert "db_pool_exhausted" in digest
    assert "2 tool results" in digest


def test_the_digest_marks_a_tool_that_actually_failed():
    digest = _partial_evidence_digest(
        [_tool_message("get_metric", "upstream 503", status="error")]
    )

    assert "(tool failed)" in digest


def test_an_empty_tool_result_is_reported_as_empty_not_omitted():
    digest = _partial_evidence_digest([_tool_message("query_logs", "")])

    assert "(empty result)" in digest


def test_a_long_tool_result_is_truncated_rather_than_dropped():
    digest = _partial_evidence_digest([_tool_message("query_logs", "x" * 5000)])

    assert "truncated" in digest
    assert len(digest) < 1200


def test_only_the_most_recent_results_survive_and_the_rest_are_counted():
    digest = _partial_evidence_digest(
        [_tool_message("query_logs", f"page {i}") for i in range(20)]
    )

    assert "20 tool results" in digest
    assert "8 older ones omitted" in digest
    assert "page 19" in digest
    assert "page 0\n" not in digest


def test_a_lane_that_collected_nothing_has_no_digest_to_offer():
    assert _partial_evidence_digest([_ai_message()]) == ""


# --- the soft deadline declines to start a doomed turn ------------------------


def test_headroom_is_reserved_for_the_measured_tail_of_a_model_call():
    # p90 specialist model-call latency on the graded run was 26.1s.
    assert _turn_headroom_seconds(120) == 30
    assert _turn_headroom_seconds(300) == 30


def test_a_short_budget_still_gets_two_thirds_of_itself_for_turns():
    # A flat 30s reservation against the 15s floor would stop the lane
    # before its first tool round.
    assert _turn_headroom_seconds(15) == 5
    assert _turn_headroom_seconds(60) == 20


def test_the_turn_limit_still_takes_precedence():
    budget = SpecialistTurnBudget(limit=2)
    budget.observe(_step(tool_call=True))

    assert (
        _cut_short_reason(budget, budget_hit=True, now=0.0, soft_deadline=1e9)
        == "turn_limit"
    )


def test_a_requested_round_is_refused_once_the_clock_runs_out():
    budget = SpecialistTurnBudget(limit=6)
    budget.observe(_step(tool_call=True))

    assert (
        _cut_short_reason(budget, budget_hit=False, now=91.0, soft_deadline=90.0)
        == "soft_deadline"
    )


def test_a_lane_about_to_write_its_report_is_never_cut_off():
    # No tool call requested means the next thing is the answer, which costs
    # nothing more. Stopping here would throw away the report itself.
    budget = SpecialistTurnBudget(limit=6)
    budget.observe(_step(tool_call=False))

    assert (
        _cut_short_reason(budget, budget_hit=False, now=1e9, soft_deadline=90.0) == ""
    )


def test_a_turn_with_time_left_proceeds():
    budget = SpecialistTurnBudget(limit=6)
    budget.observe(_step(tool_call=True))

    assert _cut_short_reason(budget, budget_hit=False, now=10.0, soft_deadline=90.0) == ""


def test_the_budget_records_whether_another_round_was_asked_for():
    budget = SpecialistTurnBudget(limit=6)

    budget.observe(_step(tool_call=True))
    assert budget.requested_another_round is True

    budget.observe(_step(tool_call=False))
    assert budget.requested_another_round is False


# --- the runbook's own query reaches the lane that can run it -----------------


def test_the_metrics_lane_is_handed_the_runbook_query():
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={
            "alert_name": "InventorySlowQueries",
            "labels": {"service": "inventory-service"},
            "annotations": {},
        },
        runbook_brief=RUNBOOK,
        runbook_query_hints=True,
    )

    assert "db_query_duration_seconds_bucket" in brief
    assert "Queries stated by the runbook" in brief
    assert "get_golden_signals" in brief
    # Still untrusted content, still wrapped.
    assert "runbook_queries" in brief


def test_the_lane_is_told_not_to_rewrite_the_runbooks_label_matchers():
    """The 2026-09-22 metrics lane relabelled `job=` to `service=`.

    The series carries `job`, so its second pass matched nothing and the
    lane concluded the metric was unavailable. Scoping is the runtime's job
    — `_scope_query` already injects the tenant namespace — so the model
    has no reason to touch a matcher, and is told so.
    """
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={"alert_name": "InventorySlowQueries", "labels": {}},
        runbook_brief=RUNBOOK,
        runbook_query_hints=True,
    )

    assert "exactly as written" in brief
    assert "add, rename or drop a label matcher" in brief
    assert "empty result" in brief


def test_the_brief_carries_the_measured_probe_when_one_was_run():
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={"alert_name": "InventorySlowQueries", "labels": {}},
        runbook_brief=RUNBOOK,
        runbook_query_hints=True,
        runbook_probe="Runbook queries already executed for you: peak 2.25",
    )

    assert "already executed for you" in brief
    assert "peak 2.25" in brief


def test_a_lane_with_a_stamped_alert_is_pointed_past_the_alert_instant():
    """Defect 1: the brief itself said to query around the alert.

    The harness stamps the alert at fault injection, so a five-minute rate
    window evaluated there is entirely pre-fault. The instruction now names
    the window to use and forbids the alert instant outright.
    """
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={
            "alert_name": "InventorySlowQueries",
            "labels": {},
            "starts_at": "2026-09-22T17:50:37Z",
        },
        runbook_brief=RUNBOOK,
        runbook_query_hints=True,
    )

    assert "Never pass the alert timestamp as the evaluation instant" in brief
    assert "start_time=" in brief and "end_time=" in brief
    assert "through the present" in brief


def test_a_lane_that_cannot_run_promql_is_not_charged_for_the_hint():
    brief = build_specialist_task_brief(
        specialist_role="Loki Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={
            "alert_name": "InventorySlowQueries",
            "labels": {"service": "inventory-service"},
            "annotations": {},
        },
        runbook_brief=RUNBOOK,
    )

    assert "Queries stated by the runbook" not in brief


def test_a_runbook_with_no_query_falls_back_to_the_metric_it_names():
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={"alert_name": "InventorySlowQueries", "labels": {}},
        runbook_brief=(
            "| inventory-service db p90 | `db_query_duration_seconds_bucket` | 1.0 s |"
        ),
        runbook_query_hints=True,
    )

    assert "Metrics named by the runbook: db_query_duration_seconds_bucket" in brief


def test_no_runbook_means_no_hint_block():
    brief = build_specialist_task_brief(
        specialist_role="Prometheus Specialist",
        objective="Investigate InventorySlowQueries",
        alert_context={"alert_name": "InventorySlowQueries", "labels": {}},
        runbook_query_hints=True,
    )

    assert "Queries stated by the runbook" not in brief
    assert "Metrics named by the runbook" not in brief


# --- the output ceiling the live transport was dropping -----------------------


def test_a_specialist_call_carries_the_configured_output_ceiling(monkeypatch):
    captured = {}

    def fake_route_llm(task_type, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("sre_agent.model_router.route_llm", fake_route_llm)

    _create_llm()

    assert (
        captured["max_tokens"] == investigation_limits().specialist_max_output_tokens
    )


def test_an_explicit_ceiling_still_wins(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "sre_agent.model_router.route_llm",
        lambda task_type, **kwargs: captured.update(kwargs) or object(),
    )

    _create_llm(max_tokens=512)

    assert captured["max_tokens"] == 512


def test_the_ceiling_is_operator_tunable_and_bounded(monkeypatch):
    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "1500")
    assert investigation_limits().specialist_max_output_tokens == 1500

    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "999999")
    assert investigation_limits().specialist_max_output_tokens == 16000

    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "nonsense")
    assert investigation_limits().specialist_max_output_tokens == 4096


def test_the_litellm_transport_no_longer_drops_the_default_ceiling(monkeypatch):
    """The provider path applied ``default_max_tokens``; this one did not.

    Every graded run uses LiteLLM, so the documented 4096-token default had
    never once been applied — which is how one specialist turn came to emit
    6,402 output tokens.
    """
    from sre_agent import litellm_backend, model_accounting, model_router

    captured = {}

    def fake_build(model, **kwargs):
        captured.update(kwargs)
        captured["model"] = model
        return object()

    monkeypatch.setattr(litellm_backend, "litellm_enabled", lambda: True)
    monkeypatch.setattr(
        litellm_backend, "tier_litellm_model", lambda *a, **k: "anthropic/claude-x"
    )
    monkeypatch.setattr(litellm_backend, "build_litellm_llm", fake_build)
    monkeypatch.setattr(model_accounting, "instrument_llm", lambda llm, **k: llm)

    model_router.route_llm(model_router.TaskType.SPECIALIST, provider="anthropic")

    assert captured["max_tokens"] == SREConstants.model.default_max_tokens


def test_an_explicit_ceiling_reaches_the_litellm_transport(monkeypatch):
    from sre_agent import litellm_backend, model_accounting, model_router

    captured = {}
    monkeypatch.setattr(litellm_backend, "litellm_enabled", lambda: True)
    monkeypatch.setattr(
        litellm_backend, "tier_litellm_model", lambda *a, **k: "anthropic/claude-x"
    )
    monkeypatch.setattr(
        litellm_backend,
        "build_litellm_llm",
        lambda model, **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(model_accounting, "instrument_llm", lambda llm, **k: llm)

    model_router.route_llm(
        model_router.TaskType.SPECIALIST, provider="anthropic", max_tokens=777
    )

    assert captured["max_tokens"] == 777


# --- the framework's step ceiling is a budget, not a crash ------------------


def test_the_step_backstop_outlives_the_turn_budget_it_backs():
    """Defect 3: the old backstop was `turns * 2 + 2`, and tripped first.

    `create_react_agent` is built here with a `pre_model_hook` node, so one
    tool round costs three LangGraph steps (hook, agent, tools) and T model
    turns need `3T - 1`. At six turns the old formula allowed 14 steps
    against the 17 the budget was meant to buy: the graceful turn counter
    could never fire, and the logs lane died mid-round instead.
    """
    turns = investigation_limits().specialist_model_turns

    assert _REACT_STEPS_PER_TURN == 3
    assert _specialist_recursion_limit(turns) >= turns * 3 - 1
    assert _specialist_recursion_limit(turns) > turns * 2 + 2
    # Still a backstop: it must not be so loose that a runaway lane is free.
    assert _specialist_recursion_limit(turns) <= turns * 3 + 2


def test_the_backstop_never_degenerates_on_a_pathological_budget():
    assert _specialist_recursion_limit(0) >= 1
    assert _specialist_recursion_limit(-4) >= 1


@pytest.fixture
def recursion_capped_lane(monkeypatch):
    """A Loki lane that hits the step ceiling after Loki has answered."""

    async def fake_astream(payload, config=None):
        yield {
            "tools": {
                "messages": [
                    _tool_message("query_logs", "db_pool_exhausted x412"),
                ]
            }
        }
        raise GraphRecursionError("Recursion limit of 14 reached")

    async def fake_artifact_metadata(state, **kwargs):
        return {}, None

    async def fake_emit(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_nodes, "_create_llm", lambda *a, **k: object())
    monkeypatch.setattr(
        agent_nodes,
        "create_react_agent",
        lambda model, tools, **kwargs: SimpleNamespace(astream=fake_astream),
    )
    monkeypatch.setattr(
        agent_nodes, "_artifact_backed_trace_metadata", fake_artifact_metadata
    )
    monkeypatch.setattr(agent_nodes, "emit_timeline_event", fake_emit)

    return agent_nodes.BaseAgentNode(
        name="Application Logs Agent", description="reads logs", tools=[]
    )


def _run_lane(node):
    return asyncio.run(
        node(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {"alert_name": "InventorySlowQueries", "labels": {}},
                "metadata": {},
                "agent_results": {},
            }
        )
    )


def test_a_lane_cut_off_by_the_step_ceiling_still_reports_its_logs(
    recursion_capped_lane,
):
    result = _run_lane(recursion_capped_lane)
    report = result["agent_results"]["logs_agent"]

    assert "db_pool_exhausted x412" in report
    assert "budget boundary, not a tool failure" in report
    assert "step ceiling" in report


def test_the_step_ceiling_is_recorded_as_its_own_cut_short_reason(
    recursion_capped_lane,
):
    result = _run_lane(recursion_capped_lane)

    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]
    assert budgets["cut_short"] == "recursion_limit"


# --- the whole lane, through the timeout that started this ------------------


@pytest.fixture
def timed_out_lane(monkeypatch):
    """A Loki lane whose model call never returns, after Loki has answered.

    This is the shape of the graded trial: the tools were fast, the model
    calls were not, and the deadline landed inside one of them.
    """
    calls = {"narrated": 0}

    async def fake_astream(payload, config=None):
        yield {
            "tools": {
                "messages": [
                    _tool_message("query_logs", "db_pool_exhausted x412"),
                    _tool_message("analyze_log_patterns", "spike at 14:03"),
                ]
            }
        }
        raise asyncio.TimeoutError()

    async def fake_artifact_metadata(state, **kwargs):
        return {}, None

    async def fake_emit(*args, **kwargs):
        return None

    async def fake_narrate(*args, **kwargs):
        calls["narrated"] += 1
        return "a teammate-sounding paraphrase"

    monkeypatch.setattr(agent_nodes, "_create_llm", lambda *a, **k: object())
    monkeypatch.setattr(
        agent_nodes,
        "create_react_agent",
        lambda model, tools, **kwargs: SimpleNamespace(astream=fake_astream),
    )
    monkeypatch.setattr(
        agent_nodes, "_artifact_backed_trace_metadata", fake_artifact_metadata
    )
    monkeypatch.setattr(agent_nodes, "emit_timeline_event", fake_emit)
    monkeypatch.setattr(agent_nodes, "narrate_specialist_finding", fake_narrate)

    node = agent_nodes.BaseAgentNode(
        name="Application Logs Agent", description="reads logs", tools=[]
    )
    return node, calls


def test_a_timed_out_lane_reports_its_logs_instead_of_reporting_none(timed_out_lane):
    node, _ = timed_out_lane

    result = asyncio.run(
        node(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {"alert_name": "InventorySlowQueries", "labels": {}},
                "metadata": {},
                "agent_results": {},
            }
        )
    )
    report = result["agent_results"]["logs_agent"]

    assert "db_pool_exhausted x412" in report
    assert "spike at 14:03" in report
    assert "budget boundary, not a tool failure" in report
    assert "wall-clock" in report


def test_a_timed_out_lane_does_not_buy_a_narration_call(timed_out_lane):
    node, calls = timed_out_lane

    asyncio.run(
        node(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {"alert_name": "InventorySlowQueries", "labels": {}},
                "metadata": {},
                "agent_results": {},
            }
        )
    )

    assert calls["narrated"] == 0


def test_the_cut_short_reason_is_recorded_for_the_run_manifest(timed_out_lane):
    node, _ = timed_out_lane

    result = asyncio.run(
        node(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {"alert_name": "InventorySlowQueries", "labels": {}},
                "metadata": {},
                "agent_results": {},
            }
        )
    )

    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]
    assert budgets["cut_short"] == "timeout"


# --- a lane that never called a tool has not investigated -------------------


def _no_tool_lane(
    monkeypatch, *, streams, narrated, name="Application Logs Agent"
):
    """Wire a BaseAgentNode to a scripted astream and count narration calls.

    ``name`` picks the lane: _get_agent_type() reads it, and the guards
    under test are keyed on the agent it resolves to.
    """

    async def fake_artifact_metadata(state, **kwargs):
        return {}, None

    async def fake_emit(*args, **kwargs):
        return None

    async def fake_narrate(*args, **kwargs):
        narrated.append(1)
        return "a teammate-sounding paraphrase"

    monkeypatch.setattr(agent_nodes, "_create_llm", lambda *a, **k: object())
    monkeypatch.setattr(
        agent_nodes,
        "create_react_agent",
        lambda model, tools, **kwargs: SimpleNamespace(astream=streams),
    )
    monkeypatch.setattr(
        agent_nodes, "_artifact_backed_trace_metadata", fake_artifact_metadata
    )
    monkeypatch.setattr(agent_nodes, "emit_timeline_event", fake_emit)
    monkeypatch.setattr(agent_nodes, "narrate_specialist_finding", fake_narrate)

    return agent_nodes.BaseAgentNode(
        name=name, description="reads logs", tools=[]
    )


@pytest.fixture
def preamble_only_lane(monkeypatch):
    """The 2026-09-22 metrics lane: one model call, a preamble, no tools.

    Nothing stopped it -- no timeout, no turn limit, no tool failure -- so
    every boundary already in place reported a lane that ran to completion,
    and its 19-character opening sentence ("I'll verify current") was handed
    on as a finding while Prometheus held the 1.59s fault that decided the
    incident.
    """
    seen, narrated = [], []

    async def fake_astream(payload, config=None):
        seen.append([str(getattr(m, "content", m)) for m in payload["messages"]])
        yield {"agent": {"messages": [AIMessage(content="I'll verify current")]}}

    return _no_tool_lane(monkeypatch, streams=fake_astream, narrated=narrated), seen, narrated


@pytest.fixture
def answers_on_retry_lane(monkeypatch):
    """The same lane, investigating properly once it is told to."""
    seen, narrated = [], []

    async def fake_astream(payload, config=None):
        seen.append([str(getattr(m, "content", m)) for m in payload["messages"]])
        if len(seen) == 1:
            yield {"agent": {"messages": [AIMessage(content="I'll verify current")]}}
            return
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="checking the histogram",
                        tool_calls=[
                            {"name": "get_metric_range", "args": {}, "id": "t1"}
                        ],
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [_tool_message("get_metric_range", "peak 1.591 at 22:10:45Z")]
            }
        }
        yield {
            "agent": {
                "messages": [
                    AIMessage(content="db p90 peaked at 1.591s, over the 1.0s threshold")
                ]
            }
        }

    return _no_tool_lane(monkeypatch, streams=fake_astream, narrated=narrated), seen, narrated


@pytest.fixture
def tool_calling_lane(monkeypatch):
    """A lane that investigated on its first pass and must not be charged twice."""
    seen, narrated = [], []

    async def fake_astream(payload, config=None):
        seen.append([str(getattr(m, "content", m)) for m in payload["messages"]])
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="looking",
                        tool_calls=[{"name": "query_logs", "args": {}, "id": "t1"}],
                    )
                ]
            }
        }
        yield {"tools": {"messages": [_tool_message("query_logs", "db_pool_exhausted")]}}
        yield {"agent": {"messages": [AIMessage(content="the pool is exhausted")]}}

    return _no_tool_lane(monkeypatch, streams=fake_astream, narrated=narrated), seen, narrated


def test_a_lane_that_called_no_tool_is_asked_again(preamble_only_lane):
    node, seen, _ = preamble_only_lane

    _run_lane(node)

    assert len(seen) == 2, "the lane was not retried"
    assert any("no tool calls" in message for message in seen[1])


def test_the_retry_answer_replaces_the_preamble(answers_on_retry_lane):
    node, seen, _ = answers_on_retry_lane

    result = _run_lane(node)
    report = result["agent_results"]["logs_agent"]

    assert len(seen) == 2
    assert "1.591s" in report
    assert "I'll verify current" not in report
    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]
    assert budgets["cut_short"] == ""


def test_a_lane_that_still_calls_nothing_is_reported_as_having_collected_none(
    preamble_only_lane,
):
    node, _, _ = preamble_only_lane

    result = _run_lane(node)
    report = result["agent_results"]["logs_agent"]
    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]

    assert budgets["cut_short"] == "no_tool_calls"
    assert "called no tools" in report
    assert "preamble rather than a finding" in report


def test_a_lane_that_collected_nothing_does_not_buy_a_narration_call(
    preamble_only_lane,
):
    node, _, narrated = preamble_only_lane

    _run_lane(node)

    assert narrated == []


def test_a_lane_that_investigated_is_never_retried(tool_calling_lane):
    node, seen, _ = tool_calling_lane

    result = _run_lane(node)
    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]

    assert len(seen) == 1
    assert budgets["cut_short"] == ""


def test_a_lane_stopped_at_a_boundary_is_not_relabelled(recursion_capped_lane):
    """A lane cut off mid-tool-round already says why; no_tool_calls would lie."""
    result = _run_lane(recursion_capped_lane)

    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]
    assert budgets["cut_short"] == "recursion_limit"


# --- the lane whose job the brief already did -------------------------------
#
# build_specialist_task_brief() inlines the authoritative runbook for the
# alert. For the runbooks lane that IS the artifact it would have gone to
# fetch, so answering from it is the contract. On 2026-09-22 the guard
# above read that as a silent lane and bought a second run of it.

_RUNBOOK = (
    "1. Confirm the slow query in pg_stat_statements.\n"
    "2. Disable the fault injection flag on inventory-service.\n"
    "3. Verify p90 returns under 1.0s."
)


def _run_runbook_lane(node, *, runbook=_RUNBOOK):
    annotations = {"runbook_context": runbook} if runbook else {}
    return asyncio.run(
        node(
            {
                "current_query": "Investigate InventorySlowQueries",
                "alert_context": {
                    "alert_name": "InventorySlowQueries",
                    "labels": {},
                    "annotations": annotations,
                },
                "metadata": {},
                "agent_results": {},
            }
        )
    )


@pytest.fixture
def runbook_lane_answering_from_the_brief(monkeypatch):
    """The runbooks lane quoting the runbook it was handed, no tool call."""
    seen, narrated = [], []

    async def fake_astream(payload, config=None):
        seen.append([str(getattr(m, "content", m)) for m in payload["messages"]])
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content=(
                            "The runbook says to disable the fault injection "
                            "flag on inventory-service, then verify p90 is "
                            "back under 1.0s."
                        )
                    )
                ]
            }
        }

    node = _no_tool_lane(
        monkeypatch,
        streams=fake_astream,
        narrated=narrated,
        name="Operational Runbooks Agent",
    )
    return node, seen, narrated


def test_the_runbooks_lane_answering_from_its_own_runbook_is_not_retried(
    runbook_lane_answering_from_the_brief,
):
    node, seen, _ = runbook_lane_answering_from_the_brief

    _run_runbook_lane(node)

    assert len(seen) == 1, "the lane was charged for a second run it did not need"


def test_the_runbooks_lane_answer_is_kept_as_a_finding(
    runbook_lane_answering_from_the_brief,
):
    node, _, narrated = runbook_lane_answering_from_the_brief

    result = _run_runbook_lane(node)
    report = result["agent_results"]["runbooks_agent"]
    budgets = result["metadata"]["specialist_turn_budgets"]["runbooks_agent"]

    assert budgets["cut_short"] == ""
    assert "called no tools" not in report
    assert "disable the fault injection" in report.lower()
    assert narrated == [1]


def test_the_runbooks_lane_with_no_runbook_in_hand_is_still_retried(
    runbook_lane_answering_from_the_brief,
):
    """With nothing inlined the lane does have to go and find a procedure,
    so the exemption is conditional on the brief, not on the lane."""
    node, seen, _ = runbook_lane_answering_from_the_brief

    result = _run_runbook_lane(node, runbook="")
    budgets = result["metadata"]["specialist_turn_budgets"]["runbooks_agent"]

    assert len(seen) == 2
    assert budgets["cut_short"] == "no_tool_calls"


def test_another_lane_handed_the_same_runbook_is_still_retried(
    preamble_only_lane,
):
    """The logs lane's evidence is in Loki. A runbook in its brief tells it
    where to look; it does not excuse it from looking."""
    node, seen, _ = preamble_only_lane

    result = _run_runbook_lane(node)
    budgets = result["metadata"]["specialist_turn_budgets"]["logs_agent"]

    assert len(seen) == 2
    assert budgets["cut_short"] == "no_tool_calls"
