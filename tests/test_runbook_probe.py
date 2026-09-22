#!/usr/bin/env python3
"""The runbook's own query, measured over the incident, not over the alert.

The 2026-09-22 ``inventory_slow_queries`` trial ran exactly the right
expression — ``histogram_quantile(0.90, sum by (le)
(rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))``,
the one the runbook names and the one the recovery oracle probes — and still
escalated a live 2.1s regression as "no action required". It evaluated it at
the alert timestamp, and the harness stamps the alert at the instant the
fault is injected, so the five-minute rate window held only pre-fault
traffic: 0.0221s against a 1.0s threshold, the runbook's healthy branch. Its
second pass then re-scoped the same query onto ``namespace``/``service``
labels the series does not carry (it carries ``job``) and got nothing back.

Three properties are held here:

* the probe window ends at the present and starts before the alert, so the
  fault is inside it wherever the alert is stamped;
* the expression is sent exactly as the runbook wrote it;
* a probe that fails, times out or matches nothing degrades to a note, never
  to an exception, because a failed measurement must not cost the lane its
  tools.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.runbook_probe import (
    MAX_PROBE_WINDOW,
    PROBE_LOOKBACK,
    metrics_probe_caller,
    probe_runbook_queries,
    probe_window,
    summarize_probe,
)

RUNBOOK = """
## Decision procedure

For InventorySlowQueries use the database histogram instead:

```
histogram_quantile(0.90, sum by (le) (rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))
```

If the measured value is below the threshold in the table, stop.
"""

ORACLE_QUERY = (
    "histogram_quantile(0.90, sum by (le) "
    '(rate(db_query_duration_seconds_bucket{job="inventory-service"}[5m])))'
)


def _range_payload(values):
    return json.dumps(
        [
            {
                "metric": {
                    "__name__": "db_query_duration_seconds_bucket",
                    "job": "inventory-service",
                },
                "values": [[ts, str(value)] for ts, value in values],
            }
        ]
    )


class _Recorder:
    """A stand-in for the lane's bound get_metric_range tool."""

    def __init__(self, result=None, error=None, delay=0.0):
        self.calls = []
        self._result = result
        self._error = error
        self._delay = delay

    async def __call__(self, tool_name, args):
        self.calls.append((tool_name, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


# --- the window covers the fault, wherever the alert is stamped --------------


def test_the_window_ends_now_not_at_the_alert():
    alert = datetime(2026, 9, 22, 17, 50, 37, tzinfo=timezone.utc)
    now = alert + timedelta(minutes=9)

    start, end = probe_window(alert, now=now)

    assert end == now, "a window that ends at the alert cannot contain the fault"
    assert start == alert - PROBE_LOOKBACK


def test_a_long_incident_is_clamped_to_the_investigation_cap():
    alert = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    now = alert + timedelta(hours=4)

    start, end = probe_window(alert, now=now)

    # namespace_scope refuses a read wider than 30 minutes; a probe that
    # asks for four hours is refused and measures nothing.
    assert end - start <= MAX_PROBE_WINDOW
    assert end == now


def test_an_alert_stamped_in_the_future_still_yields_a_forward_window():
    now = datetime(2026, 9, 22, 17, 50, 0, tzinfo=timezone.utc)
    skewed = now + timedelta(minutes=30)

    start, end = probe_window(skewed, now=now)

    assert start < end


def test_no_alert_stamp_still_probes_the_recent_past():
    now = datetime(2026, 9, 22, 17, 50, 0, tzinfo=timezone.utc)

    start, end = probe_window(None, now=now)

    assert end == now
    assert end - start == MAX_PROBE_WINDOW


# --- the runbook's expression is sent exactly as written ---------------------


def test_the_probe_sends_the_runbook_expression_verbatim():
    alert = datetime(2026, 9, 22, 17, 50, 37, tzinfo=timezone.utc)
    now = alert + timedelta(minutes=9)
    caller = _Recorder(
        result=_range_payload(
            [(now.timestamp() - 480, 0.0221), (now.timestamp(), 2.2512)]
        )
    )

    block = asyncio.run(
        probe_runbook_queries(
            RUNBOOK, tool_caller=caller, alert_started_at=alert, now=now
        )
    )

    assert len(caller.calls) == 1
    tool_name, args = caller.calls[0]
    assert tool_name == "get_metric_range"
    assert args["query"] == ORACLE_QUERY
    assert 'job="inventory-service"' in args["query"]
    assert "namespace=" not in args["query"], "the runtime scopes; the probe must not"
    assert args["end_time"] == now.isoformat()
    assert block


def test_the_block_reports_the_peak_the_alert_window_would_have_hidden():
    alert = datetime(2026, 9, 22, 17, 50, 37, tzinfo=timezone.utc)
    now = alert + timedelta(minutes=9)
    caller = _Recorder(
        result=_range_payload(
            [
                (alert.timestamp(), 0.0221),
                (alert.timestamp() + 180, 2.1019),
                (now.timestamp(), 2.2512),
            ]
        )
    )

    block = asyncio.run(
        probe_runbook_queries(
            RUNBOOK, tool_caller=caller, alert_started_at=alert, now=now
        )
    )

    assert "peak 2.251" in block
    assert "latest 2.251" in block
    assert "first 0.0221" in block
    # wrap_untrusted JSON-encodes the payload, so the quotes are escaped.
    assert "job=" in block and "inventory-service" in block
    # It is evidence from the tenant's own Prometheus, so it is fenced.
    assert "runbook_query_probe" in block


# --- failure never costs the lane anything ----------------------------------


def test_a_failing_probe_reports_the_failure_and_does_not_raise():
    caller = _Recorder(error=RuntimeError("prometheus unreachable"))

    block = asyncio.run(
        probe_runbook_queries(RUNBOOK, tool_caller=caller, alert_started_at=None)
    )

    assert "probe failed" in block
    assert "prometheus unreachable" in block


def test_a_slow_probe_is_abandoned_rather_than_holding_the_lane():
    caller = _Recorder(result="[]", delay=0.2)

    block = asyncio.run(
        probe_runbook_queries(
            RUNBOOK, tool_caller=caller, alert_started_at=None, timeout_seconds=0.01
        )
    )

    assert "timed out" in block


def test_no_metrics_tool_means_no_block_and_no_error():
    assert metrics_probe_caller([]) is None
    assert (
        asyncio.run(probe_runbook_queries(RUNBOOK, tool_caller=None)) == ""
    )


def test_a_runbook_without_promql_probes_nothing():
    caller = _Recorder(result="[]")

    block = asyncio.run(
        probe_runbook_queries("Page the on-call and wait.", tool_caller=caller)
    )

    assert block == ""
    assert caller.calls == []


# --- summaries stay readable and honest --------------------------------------


def test_an_empty_result_is_reported_as_a_finding_not_as_silence():
    assert "no series returned" in summarize_probe("[]")


def test_a_prometheus_error_string_survives_into_the_summary():
    summary = summarize_probe(
        "Error querying metric: query was rejected by Prometheus (HTTP 400)"
    )

    assert "rejected by Prometheus" in summary


def test_the_prometheus_envelope_is_understood_as_well_as_the_bare_list():
    now = datetime(2026, 9, 22, 17, 59, 0, tzinfo=timezone.utc).timestamp()
    enveloped = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"job": "inventory-service"}, "values": [[now, "2.25"]]}
                ],
            },
        }
    )

    assert "latest 2.25" in summarize_probe(enveloped)


def test_an_instant_vector_is_summarized_too():
    now = datetime(2026, 9, 22, 17, 59, 0, tzinfo=timezone.utc).timestamp()
    instant = json.dumps(
        [{"metric": {"job": "inventory-service"}, "value": [now, "2.25"]}]
    )

    assert "latest 2.25" in summarize_probe(instant)


def test_nan_and_inf_samples_do_not_become_the_reported_peak():
    """float() accepts "NaN" and "+Inf", so they must be dropped by hand.

    One NaN sample in a histogram_quantile series is ordinary — an empty
    bucket window produces it — and a single one would otherwise print the
    peak as "nan" and hide the real maximum.
    """
    now = datetime(2026, 9, 22, 17, 59, 0, tzinfo=timezone.utc).timestamp()
    payload = json.dumps(
        [
            {
                "metric": {"job": "inventory-service"},
                "values": [[now, "NaN"], [now + 30, "1.5"], [now + 60, "+Inf"]],
            }
        ]
    )

    summary = summarize_probe(payload)

    assert "peak 1.5" in summary
    assert "latest 1.5" in summary
    assert "nan" not in summary.lower()
    assert "inf" not in summary.lower()


def test_a_malformed_sample_is_skipped_not_fatal():
    now = datetime(2026, 9, 22, 17, 59, 0, tzinfo=timezone.utc).timestamp()
    payload = json.dumps(
        [
            {
                "metric": {"job": "inventory-service"},
                "values": [[now], [now + 30, "n/a"], [now + 60, "1.5"]],
            }
        ]
    )

    assert "latest 1.5" in summarize_probe(payload)


def test_the_bound_tool_caller_uses_the_lanes_own_tool():
    class _Tool:
        name = "get_metric_range"

        def __init__(self):
            self.seen = None

        async def ainvoke(self, args):
            self.seen = args
            return "[]"

    tool = _Tool()
    caller = metrics_probe_caller([tool])

    assert caller is not None
    assert asyncio.run(caller("get_metric_range", {"query": "up"})) == "[]"
    assert tool.seen == {"query": "up"}


def test_an_unbound_tool_name_raises_inside_the_caller_not_at_build_time():
    class _Tool:
        name = "get_metric_range"

        async def ainvoke(self, args):
            return "[]"

    caller = metrics_probe_caller([_Tool()])

    with pytest.raises(RuntimeError):
        asyncio.run(caller("restart_deployment", {}))


# --- the metrics lane actually starts from these numbers ---------------------


@pytest.fixture
def probing_lane(monkeypatch):
    """A metrics lane with a bound get_metric_range, wired as in production.

    Returns the node factory plus the briefs handed to create_react_agent,
    so a test can read what the model was actually told before its first
    turn.
    """
    from types import SimpleNamespace

    from langchain_core.tools import StructuredTool

    from sre_agent import agent_nodes

    briefs = []

    def _metric_range_tool():
        """A real bound tool: the node re-wraps whatever it is handed."""
        calls = []

        async def get_metric_range(
            query: str, start_time: str, end_time: str, step: str = "15s"
        ) -> str:
            calls.append(
                {
                    "query": query,
                    "start_time": start_time,
                    "end_time": end_time,
                    "step": step,
                }
            )
            return _range_payload(
                [(1758563437.0, 0.0221), (1758563977.0, 2.2512)]
            )

        tool = StructuredTool.from_function(
            coroutine=get_metric_range,
            name="get_metric_range",
            description="Range query against Prometheus.",
        )
        return tool, calls

    async def fake_astream(payload, config=None):
        briefs.append(str(payload))
        yield {"tools": {"messages": []}}

    async def fake_artifact_metadata(state, **kwargs):
        return {}, None

    async def fake_emit(*args, **kwargs):
        return None

    async def fake_narrate(*args, **kwargs):
        return "a paraphrase"

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

    def _build(name):
        tool, calls = _metric_range_tool()
        node = agent_nodes.BaseAgentNode(
            name=name, description="reads metrics", tools=[tool]
        )
        return node, calls

    return _build, briefs


def _state():
    return {
        "current_query": "Investigate InventorySlowQueries",
        "alert_context": {
            "alert_name": "InventorySlowQueries",
            "labels": {"job": "inventory-service"},
            "annotations": {"runbook_context": RUNBOOK},
            "starts_at": "2026-09-22T17:50:37Z",
        },
        "metadata": {},
        "agent_results": {},
    }


def test_the_metrics_lane_sees_the_measured_value_before_its_first_turn(
    probing_lane,
):
    build, briefs = probing_lane
    node, calls = build("Metrics Analysis Agent")

    asyncio.run(node(_state()))

    assert calls, "the lane's own bound tool should have run the query"
    assert calls[0]["query"] == ORACLE_QUERY
    assert briefs, "the lane should have been given a brief"
    brief = briefs[0]
    assert "already executed for you" in brief
    assert "2.251" in brief, "the post-fault value must reach the first turn"


def test_a_lane_that_cannot_run_promql_pays_nothing_for_the_probe(probing_lane):
    build, _ = probing_lane
    node, calls = build("Application Logs Agent")

    asyncio.run(node(_state()))

    assert calls == []


def test_a_probe_that_explodes_does_not_stop_the_lane(probing_lane, monkeypatch):
    from sre_agent import agent_nodes

    async def boom(*args, **kwargs):
        raise RuntimeError("prometheus is on fire")

    monkeypatch.setattr(agent_nodes, "probe_runbook_queries", boom)
    build, briefs = probing_lane
    node, _ = build("Metrics Analysis Agent")

    result = asyncio.run(node(_state()))

    assert "agent_results" in result
    assert briefs, "the lane still ran, with a brief, minus the probe block"
    assert "already executed for you" not in briefs[0]


# --- the gate the probe has to pass ------------------------------------------


def test_the_probes_arguments_survive_the_runtime_scope_gate():
    """The probe goes through the same wrapper as any model-issued call.

    `namespace_scope` refuses a range read wider than 30 minutes and one
    with no incident target selector. A probe whose defaults trip either
    would fail silently in production and pass every unit test here, so the
    arguments are checked against the real enforcement function.
    """
    from sre_agent.namespace_scope import _enforce_investigation_query_scope

    alert = datetime(2026, 9, 22, 17, 50, 37, tzinfo=timezone.utc)
    now = alert + timedelta(minutes=9)
    caller = _Recorder(result="[]")

    asyncio.run(
        probe_runbook_queries(
            RUNBOOK, tool_caller=caller, alert_started_at=alert, now=now
        )
    )
    _, args = caller.calls[0]

    # Raises InvestigationQueryScopeError if the window or selector is wrong.
    _enforce_investigation_query_scope("get_metric_range", args)


def test_the_widest_probe_window_still_fits_under_the_runtime_cap():
    from sre_agent.namespace_scope import MAX_INVESTIGATION_WINDOW

    assert MAX_PROBE_WINDOW < MAX_INVESTIGATION_WINDOW
