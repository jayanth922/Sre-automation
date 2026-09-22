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

from sre_agent import agent_nodes
from sre_agent.agent_nodes import (
    SpecialistTurnBudget,
    _create_llm,
    _cut_short_reason,
    _partial_evidence_digest,
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
    assert investigation_limits().specialist_max_output_tokens == 3000


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
