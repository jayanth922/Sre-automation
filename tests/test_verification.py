#!/usr/bin/env python3
"""Unit tests for remediation verification."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "sre_agent" / "verification.py"
_spec = importlib.util.spec_from_file_location("verification", _MODULE_PATH)
v = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = v
_spec.loader.exec_module(v)


def test_evaluate_resolved_and_failed():
    assert v.evaluate_verification(0.5, 0.02, 0.05).status == "RESOLVED"
    assert v.evaluate_verification(0.5, 0.40, 0.05).status == "FAILED"


def test_evaluate_improvement_pct():
    out = v.evaluate_verification(0.50, 0.10, 0.20)  # 0.10 < 0.20 → resolved
    assert out.status == "RESOLVED"
    assert out.improvement_pct == pytest.approx(80.0)


def test_evaluate_unknown_without_current_or_threshold():
    assert v.evaluate_verification(0.5, None, 0.05).status == "UNKNOWN"
    assert v.evaluate_verification(0.5, 0.02, None).status == "UNKNOWN"


def test_parse_prom_value_instant():
    assert v.parse_prom_value([{"value": [123, "0.037"]}]) == pytest.approx(0.037)


def test_parse_prom_value_range_uses_last():
    assert v.parse_prom_value([{"values": [[1, "0.9"], [2, "0.1"]]}]) == pytest.approx(0.1)


def test_parse_prom_value_json_string():
    assert v.parse_prom_value('[{"value": [1, "0.5"]}]') == pytest.approx(0.5)


def test_parse_prom_value_bad_input_is_none():
    assert v.parse_prom_value("not json") is None
    assert v.parse_prom_value([]) is None


def test_verify_remediation_resolved_via_caller():
    async def caller(tool, args):
        return [{"value": [0, "0.02"]}]

    out = asyncio.run(v.verify_remediation("sum(rate(http_errors_total[5m]))", 0.05, caller))
    assert out.status == "RESOLVED"
    assert out.current_value == pytest.approx(0.02)


def test_verify_remediation_query_failure_is_unknown():
    async def boom(tool, args):
        raise RuntimeError("prometheus down")

    out = asyncio.run(v.verify_remediation("q", 0.05, boom))
    assert out.status == "UNKNOWN"


# ── the shape Prometheus MCP really returns ──────────────────────────────────
# `prometheus_real/server.py::_cap_vector_result` wraps every instant query in
# {"result": [...], "series_total": N} to cap context. Parsing only the bare
# list is why live verification reported "no current metric value" for metrics
# that were being scraped the whole time.


def test_parse_prom_value_reads_the_capped_mcp_envelope():
    resp = '{"result":[{"value":[1,"0.42"]}],"series_returned":1,"series_total":1,"series_truncated":false}'
    assert v.parse_prom_value(resp) == pytest.approx(0.42)


def test_parse_prom_result_separates_empty_from_unreadable():
    # An empty vector is a real answer; an error string is not.
    assert v.parse_prom_result('{"result":[],"series_total":0}') == []
    assert v.parse_prom_result("[]") == []
    assert v.parse_prom_result("Error querying metric: Prometheus unreachable") is None
    assert v.parse_prom_result("not json") is None


# ── the MCP content-block envelope the live tool_caller really returns ───────
# `executor.build_mcp_tool_caller` goes through langchain_mcp_adapters, whose
# `tool.ainvoke` returns the content block as a plain dict, not a TextContent
# object. Only the object form was unwrapped, so the *envelope* — a list of one
# dict — was read as the series list. A one-element list is never empty, and
# `verify_alert_cleared` reads non-empty as "still firing": every live
# remediation was graded FAILED, even after the alert had provably cleared.

_LANGCHAIN_BLOCK = [
    {
        "type": "text",
        "text": '{"result":[],"series_returned":0,"series_total":0,"series_truncated":false}',
        "id": "lc_517c2004-b4b5-47f8-b70d-3f38ca2fbafc",
    }
]


def test_a_cleared_alert_wrapped_in_a_dict_content_block_reads_as_cleared():
    assert v.parse_prom_result(_LANGCHAIN_BLOCK) == []


def test_a_dict_content_block_still_yields_the_scalar_inside_it():
    resp = [{"type": "text", "text": '{"result":[{"value":[1,"0.42"]}],"series_total":1}'}]
    assert v.parse_prom_value(resp) == pytest.approx(0.42)


def test_the_object_form_of_a_content_block_still_works():
    """The MCP SDK's own TextContent — don't regress the shape that did work."""

    class TextContent:
        text = '{"result":[{"value":[1,"0.42"]}],"series_total":1}'

    assert v.parse_prom_value([TextContent()]) == pytest.approx(0.42)
    assert v.parse_prom_value(TextContent()) == pytest.approx(0.42)


def test_a_series_dict_is_not_mistaken_for_a_content_block():
    """Prometheus series are dicts too; only a string `text` field marks a block."""
    assert v.parse_prom_result([{"metric": {"job": "x"}, "value": [1, "2.0"]}]) == [
        {"metric": {"job": "x"}, "value": [1, "2.0"]}
    ]


def test_an_error_inside_a_content_block_is_still_unreadable():
    """Wrapping must not turn "Prometheus is down" into "the alert is firing"."""
    resp = [{"type": "text", "text": "Error querying metric: Prometheus unreachable"}]
    assert v.parse_prom_result(resp) is None


def test_a_cleared_alert_behind_the_live_envelope_verifies_as_resolved():
    """End to end through the poll loop, with the exact live response shape."""

    async def caller(tool, args):
        return _LANGCHAIN_BLOCK

    out = asyncio.run(
        v.verify_alert_cleared(
            "InventorySlowQueries", "inventory-service", caller,
            settle_seconds=0, timeout_seconds=0, poll_seconds=30,
        )
    )
    assert out.status == "RESOLVED"


def test_a_still_firing_alert_behind_the_live_envelope_verifies_as_failed():
    """The fix must not make everything resolve — a firing series still fails."""
    firing = [
        {
            "type": "text",
            "text": (
                '{"result":[{"metric":{"alertname":"InventorySlowQueries"},'
                '"value":[1,"1"]}],"series_total":1}'
            ),
        }
    ]

    async def caller(tool, args):
        return firing

    out = asyncio.run(
        v.verify_alert_cleared(
            "InventorySlowQueries", "inventory-service", caller,
            settle_seconds=0, timeout_seconds=0, poll_seconds=30,
        )
    )
    assert out.status == "FAILED"


# ── verify against the alert that opened the incident ────────────────────────


def _recording_caller(responses):
    calls = []

    async def caller(tool, args):
        calls.append(args["query"])
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return caller, calls


def test_alert_state_promql_scopes_to_the_firing_alert_and_service():
    q = v.alert_state_promql("InventorySlowQueries", "inventory-service")
    assert q == 'ALERTS{alertname="InventorySlowQueries",alertstate="firing",service="inventory-service"}'
    assert 'service=' not in v.alert_state_promql("SomeAlert", "unknown")


def test_alert_cleared_is_resolved_without_guessing_a_threshold():
    # The subject here is the *verdict*: the alert rule is the threshold, so a
    # clear reading needs no guessed comparison value. The settle floor is
    # turned off to isolate that — how long a clear reading must hold before it
    # counts is a separate question, owned by test_verification_settle_floor.py.
    caller, calls = _recording_caller(['{"result":[],"series_total":0}'])
    out = asyncio.run(
        v.verify_alert_cleared(
            "InventorySlowQueries", "inventory-service", caller, min_clear_seconds=0
        )
    )
    assert out.status == "RESOLVED"
    assert len(calls) == 1
    assert "no longer firing" in out.detail


def test_still_firing_polls_until_the_budget_runs_out_then_fails():
    slept = []

    async def sleeper(seconds):
        slept.append(seconds)

    firing = '{"result":[{"metric":{"alertname":"X"},"value":[1,"1"]}],"series_total":1}'
    caller, calls = _recording_caller([firing])
    out = asyncio.run(
        v.verify_alert_cleared(
            "X", "svc", caller, timeout_seconds=90, poll_seconds=30, sleep=sleeper
        )
    )
    assert out.status == "FAILED"
    assert "still firing after 90s" in out.detail
    assert sum(slept) == 90
    assert len(calls) == 4  # t=0, 30, 60, 90


def test_a_fix_that_lands_mid_window_resolves_without_waiting_out_the_budget():
    """A rollout takes time and the alert's own rate() window still holds the
    fault, so the first sample is expected to be stale — that is the whole
    reason this polls instead of asking once.

    The alert goes quiet at t=60 and stays quiet, so the verdict lands at the
    settle floor (120s by default) — long before the 300s budget. Early exit is
    the property under test; the floor only sets how early. Watching to the
    deadline on an alert that has clearly stopped firing would be the bug.
    """
    firing = '{"result":[{"value":[1,"1"]}],"series_total":1}'
    responses = [firing, firing, '{"result":[],"series_total":0}']
    slept = []

    async def sleeper(seconds):
        slept.append(seconds)

    async def caller(tool, args):
        return responses[min(len(slept), len(responses) - 1)]

    out = asyncio.run(
        v.verify_alert_cleared(
            "X", "svc", caller, timeout_seconds=300, poll_seconds=30, sleep=sleeper
        )
    )
    assert out.status == "RESOLVED"
    assert sum(slept) == 120 < 300
    assert "after 120s" in out.detail


def test_unreachable_prometheus_is_unknown_not_a_verdict():
    caller, _ = _recording_caller([RuntimeError("prometheus down")])
    out = asyncio.run(v.verify_alert_cleared("X", "svc", caller, timeout_seconds=0))
    assert out.status == "UNKNOWN"
    assert out.current_value is None

    caller, _ = _recording_caller(["Error querying metric: HTTP 500"])
    out = asyncio.run(v.verify_alert_cleared("X", "svc", caller, timeout_seconds=0))
    assert out.status == "UNKNOWN"


def test_settle_delay_is_honoured_before_the_first_sample():
    slept = []

    async def sleeper(seconds):
        slept.append(seconds)

    caller, _ = _recording_caller(['{"result":[],"series_total":0}'])
    out = asyncio.run(
        v.verify_alert_cleared(
            "X",
            "svc",
            caller,
            settle_seconds=45,
            timeout_seconds=300,
            # Off, so the only sleep left is the settle itself — otherwise the
            # floor's polling appends to `slept` and buries what's being asserted.
            min_clear_seconds=0,
            sleep=sleeper,
        )
    )
    assert slept == [45]
    assert out.status == "RESOLVED"
    assert "after 45s" in out.detail


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
