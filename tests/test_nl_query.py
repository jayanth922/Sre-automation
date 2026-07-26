#!/usr/bin/env python3
"""Unit tests for NL→verified-PromQL (project #3, PromptQL-style)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "nl_query.py"
_spec = importlib.util.spec_from_file_location("nl_query", _MODULE_PATH)
nl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nl
_spec.loader.exec_module(nl)


# ── intent parsing ───────────────────────────────────────────────────────────
def test_error_rate_intent():
    i = nl.parse_intent("what is the checkout error rate over the last hour?")
    assert i.metric_kind == "error_rate"
    assert i.service == "checkout-service"
    assert i.window == "1h"


def test_latency_intent_with_quantile_and_window():
    i = nl.parse_intent("show p99 latency for inventory over the last 15 minutes")
    assert i.metric_kind == "latency"
    assert i.service == "inventory-service"
    assert i.window == "15m"
    assert i.quantile == 0.99


def test_traffic_and_saturation_and_payment_intents():
    assert nl.parse_intent("what's the request rate on api gateway").metric_kind == "traffic"
    assert nl.parse_intent("checkout memory usage").metric_kind == "saturation"
    assert nl.parse_intent("how many payment failures in the last 5 minutes").metric_kind == "payment_failures"


def test_unknown_intent_returns_none():
    assert nl.parse_intent("what's the meaning of life") is None


# ── PromQL generation ────────────────────────────────────────────────────────
def test_build_error_rate_promql():
    i = nl.QueryIntent("error_rate", "checkout-service", "1h")
    q = nl.build_promql(i)
    assert 'http_errors_total{service="checkout-service"}[1h]' in q
    assert "http_requests_total" in q


def test_build_latency_promql():
    q = nl.build_promql(nl.QueryIntent("latency", "inventory-service", "5m", 0.95))
    assert q.startswith("histogram_quantile(0.95,")
    assert "http_request_duration_seconds_bucket" in q


# ── validation (the verify step) ─────────────────────────────────────────────
def test_valid_generated_queries_pass():
    for kind in ("error_rate", "latency", "traffic", "saturation", "payment_failures"):
        q = nl.build_promql(nl.QueryIntent(kind, "checkout-service", "5m"))
        ok, reason = nl.validate_promql(q)
        assert ok, f"{kind}: {reason} ({q})"


def test_service_label_key_is_not_flagged():
    ok, reason = nl.validate_promql('sum(rate(http_errors_total{service="checkout-service"}[5m]))')
    assert ok, reason


def test_reject_non_allowlisted_metric():
    ok, reason = nl.validate_promql("rate(secret_admin_metric[5m])")
    assert not ok and "non-allow-listed" in reason


def test_reject_unbounded_window():
    ok, reason = nl.validate_promql("rate(http_errors_total[48h])")
    assert not ok and "window" in reason


def test_reject_injection_and_unbalanced():
    assert nl.validate_promql("rate(http_errors_total[5m]); DROP TABLE")[0] is False
    assert nl.validate_promql("sum(rate(http_errors_total[5m])")[0] is False


# ── plan + run ───────────────────────────────────────────────────────────────
def test_plan_valid_for_known_question():
    plan = nl.plan_and_generate("checkout error rate last hour")
    assert plan.valid and plan.promql
    assert plan.steps[0] == "parse intent"


def test_plan_invalid_for_unknown_question():
    plan = nl.plan_and_generate("tell me a joke")
    assert not plan.valid


def test_run_executes_valid_query_via_tool_caller():
    calls = {}

    async def fake(tool, args):
        calls["tool"] = tool
        calls["query"] = args["query"]
        return [{"value": [0, "0.03"]}]

    res = asyncio.run(nl.run_nl_query("checkout error rate last hour", tool_caller=fake))
    assert res.executed is True
    assert calls["tool"] == "get_metric"
    assert res.data == [{"value": [0, "0.03"]}]


def test_run_does_not_execute_invalid_query():
    async def fake(tool, args):
        raise AssertionError("should not be called")

    res = asyncio.run(nl.run_nl_query("tell me a joke", tool_caller=fake))
    assert res.executed is False and res.error


def test_run_without_caller_is_not_executed():
    res = asyncio.run(nl.run_nl_query("checkout error rate", tool_caller=None))
    assert res.executed is False


# ── chat routing ─────────────────────────────────────────────────────────────
def test_classify_chat_message_modes():
    assert nl.classify_chat_message("hi")["mode"] == "greeting"
    assert nl.classify_chat_message("what is the checkout error rate")["mode"] == "query"
    assert nl.classify_chat_message("focus on the logs")["mode"] == "steer"
    assert nl.classify_chat_message("please roll it back")["mode"] == "steer"


def test_incident_message_payload():
    path, body = nl.build_incident_message_payload("inc-1", "focus on logs")
    assert path == "/api/v1/incidents/inc-1/message"
    assert body == {"message": "focus on logs"}


def test_handle_chat_message_steer_builds_post():
    out = asyncio.run(nl.handle_chat_message("focus on the logs", incident_id="inc-1"))
    assert out["mode"] == "steer"
    assert out["post"]["path"] == "/api/v1/incidents/inc-1/message"


def test_handle_chat_message_greeting():
    out = asyncio.run(nl.handle_chat_message("hi"))
    assert out["mode"] == "greeting"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
