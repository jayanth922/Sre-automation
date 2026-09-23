#!/usr/bin/env python3
"""A lane that writes nothing must not delete what the runtime measured.

The 2026-09-23 ``inventory_slow_queries`` trial diagnosed nothing and closed
UNRESOLVED, and no component failed. Two lanes produced zero characters:

* Performance Metrics ran two model calls -- 31.7s and 34.4s, the longest of
  the run -- and emitted neither text nor a tool call either time. Its brief
  already carried the runtime's own pre-executed probe, ``peak 2.023`` against
  the runbook's 1.0s branch threshold: the one number the incident turned on.
  The probe was passed inline and never kept, so it died with the lane.
* Application Logs called ten tools across six turns and never wrote a text
  block. All ten results were discarded and replaced by the budget note.

The reflector was then handed no evidence at all and correctly reported that
the gating measurement had never been taken. It had been taken -- by us,
deterministically, before either lane's first turn.

Underneath both: the balanced tier is an extended-thinking model, thinking is
billed out of ``max_tokens``, and nothing in this lane ever read the
provider's stop reason -- so a turn cut off at the ceiling and a model that
declined to investigate produced byte-identical reports.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from sre_agent import agent_nodes
from sre_agent.agent_nodes import (
    _EVIDENCE_DIGEST_HEADER,
    _NO_TOOL_LANE_NOTE,
    _NO_TOOL_RETRY_DIRECTIVE,
    _SALVAGE_MAX_CHARS,
    _TRUNCATED_LANE_NOTE,
    _TRUNCATED_RETRY_DIRECTIVE,
    _TRUNCATION_FINISH_REASONS,
    _finish_reason,
    _probe_measurement_note,
    _salvaged_evidence,
)
from sre_agent.context_compaction import DEFAULT_RESERVED_OUTPUT_TOKENS
from sre_agent.investigation_limits import investigation_limits
from sre_agent.narrative import _truncate
from sre_agent.prompt_guard import wrap_untrusted
from sre_agent.runbook_probe import probe_payload

MEASUREMENT = (
    "histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket"
    '{job="inventory-service"}[5m]))) over 02:23:00Z -> 02:38:00Z: '
    "peak 2.023 at 2026-09-23T02:33:09Z, latest 1.981, samples 31"
)


def _probe_block(payload: str = MEASUREMENT) -> str:
    """A block shaped exactly as runbook_probe.probe_runbook_queries builds it."""
    return "\n".join(
        [
            "Runbook queries already executed for you, verbatim. Read the "
            "runbook's thresholds against THESE numbers.",
            wrap_untrusted("runbook_query_probe", payload, max_len=len(payload) + 1),
        ]
    )


def _tool_message(name, content, *, status="success"):
    return SimpleNamespace(
        tool_call_id=f"call-{name}", name=name, content=content, status=status
    )


# --- fix 1: the probe measurement is evidence, not prompt decoration --------


def test_the_probe_measurement_survives_its_own_envelope():
    assert probe_payload(_probe_block()) == MEASUREMENT


def test_a_block_that_is_not_a_probe_yields_nothing_rather_than_guessing():
    assert probe_payload("") == ""
    assert probe_payload("no envelope here at all") == ""
    assert probe_payload("<<UNTRUSTED_EVIDENCE_V1 -->>\nnot json\n") == ""
    other = "\n".join(
        [
            "header",
            wrap_untrusted("query_logs", "some log line", max_len=200),
        ]
    )
    assert probe_payload(other) == ""


def test_the_salvaged_probe_is_labelled_as_the_runtimes_own_arithmetic():
    note = _probe_measurement_note(_probe_block())

    assert "peak 2.023" in note
    assert "measured by the runtime" in note
    assert "not a model's claim" in note


def test_a_lane_with_no_probe_and_no_tools_salvages_nothing():
    assert _salvaged_evidence([], "") == ""


def test_a_salvaged_probe_survives_the_1800_char_cap_it_will_meet_downstream():
    """narrative._truncate is the only reason salvage cannot grow the payload."""
    note = _probe_measurement_note(_probe_block())

    assert len(note) <= _SALVAGE_MAX_CHARS + 200
    assert "peak 2.023" in _truncate(note)


def test_an_overlong_probe_is_capped_rather_than_carried_whole():
    note = _probe_measurement_note(_probe_block("x" * 40_000))

    assert len(note) < _SALVAGE_MAX_CHARS + 200


# --- fix 2: the provider's stop reason ---------------------------------------


def test_the_stop_reason_is_read_from_where_litellm_puts_it():
    assert (
        _finish_reason(
            SimpleNamespace(response_metadata={"finish_reason": "LENGTH"})
        )
        == "length"
    )
    assert (
        _finish_reason(SimpleNamespace(additional_kwargs={"stop_reason": "max_tokens"}))
        == "max_tokens"
    )


def test_a_message_carrying_no_stop_reason_reports_none_rather_than_a_default():
    assert _finish_reason(SimpleNamespace()) == ""
    assert _finish_reason(SimpleNamespace(response_metadata=None)) == ""
    assert _finish_reason(SimpleNamespace(response_metadata={})) == ""
    assert _finish_reason(SimpleNamespace(response_metadata={"finish_reason": ""})) == ""


def test_only_a_ceiling_stop_counts_as_truncation():
    assert "length" in _TRUNCATION_FINISH_REASONS
    assert "max_tokens" in _TRUNCATION_FINISH_REASONS
    assert "stop" not in _TRUNCATION_FINISH_REASONS
    assert "end_turn" not in _TRUNCATION_FINISH_REASONS
    assert "tool_use" not in _TRUNCATION_FINISH_REASONS


# --- fix 3: the ceiling itself ------------------------------------------------


def test_the_specialist_output_ceiling_meets_the_reservation_already_made_for_it():
    """The compactor subtracts 4096 from every input budget on this path.

    Capping output below that reserved nothing extra and merely made the
    reservation unusable -- while extended thinking spent the smaller
    allowance before any text was written.
    """
    assert (
        investigation_limits().specialist_max_output_tokens
        == DEFAULT_RESERVED_OUTPUT_TOKENS
    )
    assert investigation_limits().specialist_max_output_tokens == 4096


def test_the_ceiling_is_still_operator_settable_and_still_bounded(monkeypatch):
    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "9000")
    assert investigation_limits().specialist_max_output_tokens == 9000

    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "999999")
    assert investigation_limits().specialist_max_output_tokens == 16000

    monkeypatch.setenv("SPECIALIST_MAX_OUTPUT_TOKENS", "not-a-number")
    assert investigation_limits().specialist_max_output_tokens == 4096


# --- the whole lane ------------------------------------------------------------


def _lane(monkeypatch, fake_astream, *, name, agent_type):
    async def fake_artifact_metadata(state, **kwargs):
        return {}, None

    async def fake_emit(*args, **kwargs):
        return None

    async def fake_narrate(*args, **kwargs):
        return ""

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

    node = agent_nodes.BaseAgentNode(name=name, description="d", tools=[])
    node.agent_type = agent_type
    return node


def _run(node):
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


@pytest.fixture
def truncated_metrics_lane(monkeypatch):
    """The Performance Metrics lane of 2026-09-23: two turns, zero characters."""
    seen = {"directives": []}

    async def fake_probe(self, state):
        return _probe_block()

    async def fake_astream(payload, config=None):
        seen["directives"].append(
            " ".join(str(getattr(m, "content", "")) for m in payload["messages"])
        )
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="", response_metadata={"finish_reason": "length"}
                    )
                ]
            }
        }

    monkeypatch.setattr(
        agent_nodes.BaseAgentNode, "_probe_runbook_queries", fake_probe
    )
    node = _lane(
        monkeypatch,
        fake_astream,
        name="Performance Metrics Agent",
        agent_type="metrics",
    )
    return node, seen


def test_a_lane_cut_off_at_the_ceiling_still_reports_the_measured_p90(
    truncated_metrics_lane,
):
    node, _ = truncated_metrics_lane

    report = _run(node)["agent_results"]["metrics_agent"]

    assert "peak 2.023" in report
    assert "measured by the runtime" in report


def test_truncation_is_recorded_as_itself_not_as_a_lane_that_skipped_its_work(
    truncated_metrics_lane,
):
    node, _ = truncated_metrics_lane

    result = _run(node)
    budget = result["metadata"]["specialist_turn_budgets"]["metrics_agent"]
    report = result["agent_results"]["metrics_agent"]

    assert budget["cut_short"] == "output_truncated"
    assert _TRUNCATED_LANE_NOTE in report
    assert _NO_TOOL_LANE_NOTE not in report


def test_the_retry_asks_a_truncated_lane_for_brevity_not_for_evidence(
    truncated_metrics_lane,
):
    """The old directive told a model cut off mid-sentence not to answer from
    the brief. It never reached an answer; brevity is the only thing that
    makes the second attempt fit."""
    node, seen = truncated_metrics_lane

    _run(node)

    assert len(seen["directives"]) == 2, "the no-tool retry should still fire"
    assert _TRUNCATED_RETRY_DIRECTIVE in seen["directives"][1]
    assert _NO_TOOL_RETRY_DIRECTIVE not in seen["directives"][1]


def test_an_untruncated_silent_lane_keeps_the_original_directive(monkeypatch):
    seen = {"directives": []}

    async def fake_astream(payload, config=None):
        seen["directives"].append(
            " ".join(str(getattr(m, "content", "")) for m in payload["messages"])
        )
        yield {"agent": {"messages": [AIMessage(content="I will begin shortly.")]}}

    node = _lane(
        monkeypatch, fake_astream, name="Kubernetes Agent", agent_type="kubernetes"
    )
    result = _run(node)

    assert len(seen["directives"]) == 2
    assert _NO_TOOL_RETRY_DIRECTIVE in seen["directives"][1]
    assert _TRUNCATED_RETRY_DIRECTIVE not in seen["directives"][1]
    assert (
        result["metadata"]["specialist_turn_budgets"]["kubernetes_agent"]["cut_short"]
        == "no_tool_calls"
    )


# --- fix 4: ten tool results are not nothing ----------------------------------


@pytest.fixture
def silent_logs_lane(monkeypatch):
    """The Application Logs lane: tools answered, the model never wrote it up."""

    async def fake_astream(payload, config=None):
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "query_logs", "args": {}, "id": "call-1"}
                        ],
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [
                    _tool_message("query_logs", "db_pool_exhausted x412"),
                    _tool_message("analyze_log_patterns", "top pattern: pool timeout"),
                ]
            }
        }

    return _lane(
        monkeypatch, fake_astream, name="Application Logs Agent", agent_type="logs"
    )


def test_a_lane_that_never_wrote_a_finding_still_reports_what_its_tools_returned(
    silent_logs_lane,
):
    report = _run(silent_logs_lane)["agent_results"]["logs_agent"]

    assert "db_pool_exhausted x412" in report
    assert "top pattern: pool timeout" in report
    assert "stopped without writing a finding" in report


def test_salvage_is_capped_below_the_cut_it_will_meet_downstream():
    """The whole point of the constraint: salvage cannot grow the payload.

    _partial_evidence_digest alone is bounded at 600 chars x 12 results, or
    about 7.2KB -- right for the cut-short paths, where it replaces an entire
    lane report, and far too much here, where it is added to one. Salvage
    therefore carries its own ceiling, under the 1800 chars
    narrative._truncate leaves of a finding entering the next lane's brief.
    """
    salvage = _salvaged_evidence(
        [_tool_message(f"tool_{i}", "y" * 4000) for i in range(40)],
        _probe_block(),
    )

    assert len(salvage) <= _SALVAGE_MAX_CHARS + 200
    assert len(salvage) < len(_truncate(salvage)) + 200
    assert "further line(s) omitted here" in salvage
    # The decisive number is never the part that gets cut.
    assert "peak 2.023" in salvage


def test_a_probe_alone_cannot_crowd_out_the_tool_results():
    salvage = _salvaged_evidence(
        [_tool_message("query_logs", "db_pool_exhausted x412")],
        _probe_block("z" * 40_000),
    )

    assert "db_pool_exhausted x412" in salvage
    assert len(salvage) <= _SALVAGE_MAX_CHARS + 200


def test_salvage_never_repeats_a_digest_the_lane_is_already_carrying():
    """The timeout and recursion handlers append the same digest themselves."""
    messages = [_tool_message("query_logs", "db_pool_exhausted x412")]
    already = agent_nodes._partial_evidence_digest(messages)

    assert _EVIDENCE_DIGEST_HEADER in already
    assert _salvaged_evidence(messages, "", already) == ""


def test_a_recursion_capped_lane_reports_its_logs_exactly_once(monkeypatch):
    async def fake_astream(payload, config=None):
        yield {
            "tools": {"messages": [_tool_message("query_logs", "db_pool_exhausted")]}
        }
        raise GraphRecursionError("Recursion limit of 14 reached")

    node = _lane(
        monkeypatch, fake_astream, name="Application Logs Agent", agent_type="logs"
    )
    report = _run(node)["agent_results"]["logs_agent"]

    assert report.count("db_pool_exhausted") == 1


def test_a_lane_that_did_write_a_finding_is_left_alone(monkeypatch):
    async def fake_astream(payload, config=None):
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "query_logs", "args": {}, "id": "call-1"}
                        ],
                    )
                ]
            }
        }
        yield {"tools": {"messages": [_tool_message("query_logs", "raw log text")]}}
        yield {
            "agent": {
                "messages": [AIMessage(content="The pool is exhausted at 412 waiters.")]
            }
        }

    node = _lane(
        monkeypatch, fake_astream, name="Application Logs Agent", agent_type="logs"
    )
    report = _run(node)["agent_results"]["logs_agent"]

    assert "The pool is exhausted at 412 waiters." in report
    assert _EVIDENCE_DIGEST_HEADER not in report
    assert "raw log text" not in report
