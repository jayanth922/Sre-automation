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


# ── production-grade augmentation: live catalog, real parser, LLM fallback ──
class _FakeLLM:
    """Minimal stand-in for a langchain chat model's .ainvoke()."""

    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages):
        class _Resp:
            def __init__(self, content):
                self.content = content

        return _Resp(self._content)


def test_fetch_metric_catalog_returns_frozenset_from_tool():
    async def fake(tool, args):
        assert tool == "list_metric_names"
        return {"metric_names": ["http_requests_total", "custom_business_metric"]}

    catalog = asyncio.run(nl.fetch_metric_catalog(fake))
    assert catalog == frozenset({"http_requests_total", "custom_business_metric"})


def test_fetch_metric_catalog_returns_none_on_failure():
    async def fake(tool, args):
        raise RuntimeError("mcp unreachable")

    assert asyncio.run(nl.fetch_metric_catalog(fake)) is None


def test_validate_promql_with_live_catalog_allows_new_metric():
    catalog = frozenset({"custom_business_metric"})
    ok, reason = nl.validate_promql("sum(custom_business_metric)", allowed_metrics=catalog)
    assert ok, reason


def test_validate_promql_with_live_catalog_rejects_stale_static_metric():
    # A metric from the static allow-list that the live catalog doesn't have
    # (e.g. removed/renamed) must now be rejected — catalog grounding makes
    # validation stricter, not looser.
    catalog = frozenset({"custom_business_metric"})
    ok, reason = nl.validate_promql("rate(http_errors_total[5m])", allowed_metrics=catalog)
    assert not ok and "non-allow-listed" in reason


def test_validate_promql_syntax_live_ok():
    async def fake(tool, args):
        assert tool == "validate_promql_syntax"
        return {"valid": True}

    ok, reason = asyncio.run(nl.validate_promql_syntax_live("sum(up)", fake))
    assert ok, reason


def test_validate_promql_syntax_live_rejects_parser_error():
    async def fake(tool, args):
        return {"valid": False, "error": "unexpected token"}

    ok, reason = asyncio.run(nl.validate_promql_syntax_live("sum(up", fake))
    assert not ok and "unexpected token" in reason


def test_validate_promql_syntax_live_fails_open_on_tool_error():
    async def fake(tool, args):
        raise RuntimeError("tool unavailable")

    ok, reason = asyncio.run(nl.validate_promql_syntax_live("sum(up)", fake))
    assert ok  # fails open: an unreachable checker must not block otherwise-valid queries


def test_generate_promql_llm_returns_single_line_query():
    llm = _FakeLLM("```promql\nsum(rate(http_errors_total[5m]))\n```")
    q = asyncio.run(nl.generate_promql_llm("weird one-off question", llm))
    assert q == "sum(rate(http_errors_total[5m]))"


def test_generate_promql_llm_returns_none_on_failure():
    class _BrokenLLM:
        async def ainvoke(self, messages):
            raise RuntimeError("provider down")

    assert asyncio.run(nl.generate_promql_llm("anything", _BrokenLLM())) is None


def test_plan_and_generate_verified_matches_deterministic_plan_by_default():
    # With every optional arg at default, must match plan_and_generate exactly.
    verified = asyncio.run(nl.plan_and_generate_verified("checkout error rate last hour"))
    deterministic = nl.plan_and_generate("checkout error rate last hour")
    assert verified.promql == deterministic.promql
    assert verified.valid == deterministic.valid
    assert verified.generated_by == "template"


def test_plan_and_generate_verified_falls_back_to_llm_for_unmapped_question():
    llm = _FakeLLM("sum(rate(http_errors_total[5m]))")
    plan = asyncio.run(nl.plan_and_generate_verified("tell me a joke", llm=llm))
    assert plan.valid and plan.generated_by == "llm"
    assert plan.promql == "sum(rate(http_errors_total[5m]))"


def test_plan_and_generate_verified_llm_output_still_gets_validated():
    llm = _FakeLLM("rate(secret_admin_metric[5m])")
    plan = asyncio.run(nl.plan_and_generate_verified("tell me a joke", llm=llm))
    assert not plan.valid and "non-allow-listed" in plan.reason


def test_plan_and_generate_verified_without_llm_stays_invalid_for_unmapped_question():
    plan = asyncio.run(nl.plan_and_generate_verified("tell me a joke"))
    assert not plan.valid


# ── run_nl_query: default-path regression + new opt-in behavior ─────────────
def test_run_nl_query_default_path_unchanged_calls_only_get_metric():
    """Regression guard: passing none of the new kwargs must reproduce the
    exact prior behavior — one get_metric call, nothing else."""
    calls = []

    async def fake(tool, args):
        calls.append(tool)
        return [{"value": [0, "0.03"]}]

    res = asyncio.run(nl.run_nl_query("checkout error rate last hour", tool_caller=fake))
    assert res.executed is True
    assert calls == ["get_metric"]


def test_run_nl_query_invalid_question_still_makes_zero_calls_by_default():
    async def fake(tool, args):
        raise AssertionError("should not be called")

    res = asyncio.run(nl.run_nl_query("tell me a joke", tool_caller=fake))
    assert res.executed is False and res.error


def test_run_nl_query_with_live_catalog_fetches_then_executes():
    calls = []

    async def fake(tool, args):
        calls.append(tool)
        if tool == "list_metric_names":
            return {"metric_names": sorted(nl._ALLOWED_METRICS)}
        return [{"value": [0, "0.03"]}]

    res = asyncio.run(
        nl.run_nl_query("checkout error rate last hour", tool_caller=fake, use_live_catalog=True)
    )
    assert res.executed is True
    assert calls == ["list_metric_names", "get_metric"]


def test_run_nl_query_with_llm_fallback_executes_generated_query():
    calls = []
    llm = _FakeLLM("sum(rate(http_errors_total[5m]))")

    async def fake(tool, args):
        calls.append(tool)
        return [{"value": [0, "0.01"]}]

    res = asyncio.run(nl.run_nl_query("tell me a joke", tool_caller=fake, llm=llm))
    assert res.executed is True
    assert res.plan.generated_by == "llm"
    assert calls == ["get_metric"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
