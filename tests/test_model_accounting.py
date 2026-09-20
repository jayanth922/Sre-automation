#!/usr/bin/env python3
"""A08 tests for trace-linked, fail-closed routed-model accounting."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "sre_agent" / "model_accounting.py"
_spec = importlib.util.spec_from_file_location("model_accounting", MODULE_PATH)
accounting = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = accounting
_spec.loader.exec_module(accounting)

TRACE = {
    "root_trace_id": "trace-123",
    "run_manifest_id": "manifest-123",
    "incident_id": "incident-123",
    "job_id": "job-123",
}


class FakeModel:
    def __init__(self, provider="groq", model_name="model-a"):
        self.provider = provider
        self.model_name = model_name
        self.callbacks = []


def _response(*, provider="groq", model="model-a", cost=0.0125):
    response_metadata = {
        "model_provider": provider,
        "model_name": model,
    }
    if cost is not None:
        response_metadata["response_cost"] = cost
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
        response_metadata=response_metadata,
    )
    generation = SimpleNamespace(message=message)
    return SimpleNamespace(llm_output={}, generations=[[generation]])


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ACCOUNTING_PATH", str(tmp_path / "calls.jsonl"))
    accounting.get_model_accounting_recorder().reset()


def _instrument(model, *, requested_provider="groq", requested_model="model-a"):
    accounting.instrument_llm(
        model,
        task_type="planning",
        tier="strong",
        requested_provider=requested_provider,
        requested_model=requested_model,
        fallback_allowed=True,
    )
    return model.callbacks[-1]


def test_complete_call_records_actual_route_usage_cost_latency_and_trace(tmp_path):
    model = FakeModel()
    callback = _instrument(model)

    callback.on_chat_model_start(
        {}, [["secret prompt must not be recorded"]], run_id="call-1", metadata=TRACE
    )
    callback.on_llm_end(_response(), run_id="call-1")
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )
    record = accounting.get_model_accounting_recorder().records()[0]
    artifact_text = (tmp_path / "calls.jsonl").read_text()
    artifact = [json.loads(line) for line in artifact_text.splitlines()]

    assert summary["complete"] is True
    assert summary["cost_usd"] == pytest.approx(0.0125)
    assert summary["tokens"] == {
        "input": 10,
        "output": 4,
        "total": 14,
        "cache_read": 0,
        "cache_creation": 0,
    }
    assert summary["root_trace_ids"] == ["trace-123"]
    assert record["routing"]["actual_provider"] == "groq"
    assert record["trace"] == TRACE
    assert [item["record_type"] for item in artifact] == [
        "model_call",
        "trace_finalization",
    ]
    assert "secret prompt" not in artifact_text


def test_unpriceable_model_fails_closed_instead_of_estimating():
    """``model-a`` is in no price table, so there is nothing to derive from."""
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-2", metadata=TRACE)
    callback.on_llm_end(_response(cost=None), run_id="call-2")
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    summary = accounting.get_model_accounting_recorder().summary(
        incident_id="incident-123"
    )

    assert summary["complete"] is False
    assert summary["cost_usd"] is None
    assert any(
        "cost_unavailable" in reason for reason in summary["completeness_reasons"]
    )


def test_provider_reported_cost_is_labelled_and_never_recomputed(monkeypatch):
    """A reported cost wins outright; the price table is not consulted."""
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: pytest.fail("price table consulted despite a reported cost"),
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-p", metadata=TRACE)
    callback.on_llm_end(_response(cost=0.0125), run_id="call-p")

    record = accounting.get_model_accounting_recorder().records()[0]
    assert record["cost_usd"] == pytest.approx(0.0125)
    assert record["cost_source"] == "provider"
    assert record["cost_rates"] is None


def test_cost_is_derived_from_tokens_when_the_client_hides_response_cost(monkeypatch):
    """ChatLiteLLM drops ``_hidden_params``; tokens still price the call."""
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-d", metadata=TRACE)
    callback.on_llm_end(_response(cost=None), run_id="call-d")
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    record = accounting.get_model_accounting_recorder().records()[0]
    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )

    # 10 input @ $2/M + 4 output @ $10/M
    assert record["cost_usd"] == pytest.approx(10 * 2e-6 + 4 * 1e-5)
    assert record["cost_source"] == "derived"
    assert record["cost_rates"]["input_cost_per_token"] == pytest.approx(2e-6)
    assert summary["complete"] is True
    assert summary["cost_sources"] == ["derived"]


def test_cached_input_is_priced_at_the_cache_rate_not_the_input_rate(monkeypatch):
    """LiteLLM folds cache tokens into ``prompt_tokens``; they are not new input."""
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: {
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 2e-7,
            "cache_creation_input_token_cost": 2.5e-6,
        },
    )
    response = _response(cost=None)
    response.generations[0][0].message.usage_metadata.update(
        {"input_tokens": 100, "input_token_details": {"cache_read": 60}}
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-c", metadata=TRACE)
    callback.on_llm_end(response, run_id="call-c")

    record = accounting.get_model_accounting_recorder().records()[0]
    # 40 uncached @ $2/M + 60 cached @ $0.20/M + 4 output @ $10/M
    assert record["cost_usd"] == pytest.approx(40 * 2e-6 + 60 * 2e-7 + 4 * 1e-5)


def test_cache_breakdown_exceeding_the_input_count_is_not_priced(monkeypatch):
    """Contradictory usage must fail closed rather than invent a negative charge."""
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
    )
    response = _response(cost=None)
    response.generations[0][0].message.usage_metadata.update(
        {"input_tokens": 10, "input_token_details": {"cache_read": 99}}
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-x", metadata=TRACE)
    callback.on_llm_end(response, run_id="call-x")

    record = accounting.get_model_accounting_recorder().records()[0]
    assert record["cost_usd"] is None
    assert record["cost_source"] is None
    assert "cost_unavailable" in record["completeness_reasons"]


def _priced_cache_write(monkeypatch, ttl):
    """Record one call that writes 50 cache tokens, under cache TTL ``ttl``."""
    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE_TTL", ttl)
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: {
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 1e-5,
            "cache_read_input_token_cost": 2e-7,
            "cache_creation_input_token_cost": 2.5e-6,
            "cache_creation_input_token_cost_above_1hr": 4e-6,
        },
    )
    response = _response(cost=None)
    response.generations[0][0].message.usage_metadata.update(
        {"input_tokens": 50, "input_token_details": {"cache_creation": 50}}
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id=f"w-{ttl}", metadata=TRACE)
    callback.on_llm_end(response, run_id=f"w-{ttl}")
    return accounting.get_model_accounting_recorder().records()[0]


def test_one_hour_cache_writes_are_priced_at_the_one_hour_rate(monkeypatch):
    """A longer-lived cache entry costs more to write (2x base, not 1.25x).
    5m is the router's default, but an operator can still ask for 1h, and
    pricing that write at LiteLLM's 5m key would understate the bill by 60%."""
    record = _priced_cache_write(monkeypatch, "1h")

    assert record["cost_usd"] == pytest.approx(50 * 4e-6 + 4 * 1e-5)
    assert record["cost_rates"]["cache_creation_input_token_cost"] == 4e-6


def test_five_minute_cache_writes_are_priced_at_the_five_minute_rate(monkeypatch):
    record = _priced_cache_write(monkeypatch, "5m")

    assert record["cost_usd"] == pytest.approx(50 * 2.5e-6 + 4 * 1e-5)
    assert record["cost_rates"]["cache_creation_input_token_cost"] == 2.5e-6


def test_cache_token_breakdown_is_persisted_on_the_record(monkeypatch):
    """Without these the derived cost cannot be re-checked against its rates,
    and a run cannot show what its prompt cache bought."""
    monkeypatch.setattr(
        accounting,
        "_price_table_entry",
        lambda model: {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
    )
    response = _response(cost=None)
    response.generations[0][0].message.usage_metadata.update(
        {
            "input_tokens": 100,
            "input_token_details": {"cache_read": 60, "cache_creation": 25},
        }
    )
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-b", metadata=TRACE)
    callback.on_llm_end(response, run_id="call-b")

    tokens = accounting.get_model_accounting_recorder().records()[0]["tokens"]
    assert tokens["cache_read"] == 60
    assert tokens["cache_creation"] == 25
    # A breakdown of the input count, never an addition to it.
    assert tokens["cache_read"] + tokens["cache_creation"] <= tokens["input"]


def test_absent_cache_reporting_is_recorded_as_none_not_zero(monkeypatch):
    """Most providers report no cache usage at all. That is unknown, not zero,
    and it is not a completeness failure either."""
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-n", metadata=TRACE)
    callback.on_llm_end(_response(), run_id="call-n")

    record = accounting.get_model_accounting_recorder().records()[0]
    assert record["tokens"]["cache_read"] is None
    assert record["tokens"]["cache_creation"] is None
    assert record["completeness_reasons"] == []


def test_provider_qualified_model_names_resolve_in_the_price_table():
    """The router sends ``anthropic/claude-…``; the table is keyed on the bare id."""
    pytest.importorskip("litellm")
    from litellm import model_cost

    bare = next(
        (
            name
            for name, entry in model_cost.items()
            if "/" not in name
            and isinstance(entry, dict)
            and entry.get("input_cost_per_token") is not None
        ),
        None,
    )
    assert bare is not None, "litellm shipped no priced, unqualified model"
    assert accounting._price_table_entry(f"anthropic/{bare}") == model_cost[bare]
    assert accounting._price_table_entry("no/such-model-xyz") is None


def test_actual_provider_change_is_explicit_fallback_evidence():
    callback = _instrument(
        FakeModel(provider="anthropic", model_name="claude-fallback")
    )
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-3", metadata=TRACE)
    callback.on_llm_end(
        _response(provider="anthropic", model="claude-fallback"),
        run_id="call-3",
    )
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )

    assert summary["complete"] is True
    assert summary["fallbacks"] == [
        {
            "call_id": "call-3",
            "from": "groq",
            "to_provider": "anthropic",
            "to_model": "claude-fallback",
        }
    ]


def test_router_integration_name_is_not_mistaken_for_a_fallback():
    """LangChain reports the integration (``litellm``), never the served provider.

    Every record on the live stack claimed ``fallback_from: anthropic`` while
    reaching Anthropic exactly as requested. The route is recoverable from the
    qualified model id the router emits.
    """
    callback = _instrument(
        FakeModel(provider="litellm", model_name="anthropic/claude-sonnet-5"),
        requested_provider="anthropic",
        requested_model="claude-sonnet-5",
    )
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-r1", metadata=TRACE)
    callback.on_llm_end(
        _response(provider="litellm", model="anthropic/claude-sonnet-5"),
        run_id="call-r1",
    )
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    record = accounting.get_model_accounting_recorder().records()[0]
    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )

    assert record["routing"]["fallback_from"] is None
    assert summary["fallbacks"] == []
    # What was observed is still recorded verbatim: the derived-cost lookup is
    # keyed on the qualified model id, so normalising it here would be a
    # pricing change disguised as a labelling fix.
    assert record["routing"]["actual_provider"] == "litellm"
    assert record["routing"]["actual_model"] == "anthropic/claude-sonnet-5"


def test_router_reaching_another_provider_is_still_a_fallback():
    """Resolving the wrapper must not blind the check it exists to perform."""
    callback = _instrument(
        FakeModel(provider="litellm", model_name="groq/llama-3.3-70b"),
        requested_provider="anthropic",
        requested_model="claude-sonnet-5",
    )
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-r2", metadata=TRACE)
    callback.on_llm_end(
        _response(provider="litellm", model="groq/llama-3.3-70b"),
        run_id="call-r2",
    )
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    record = accounting.get_model_accounting_recorder().records()[0]
    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )

    assert record["routing"]["fallback_from"] == "anthropic"
    assert [item["from"] for item in summary["fallbacks"]] == ["anthropic"]


def test_router_without_a_recoverable_provider_claims_nothing():
    """An unknown route is unknown; it is not evidence of a fallback."""
    callback = _instrument(
        FakeModel(provider="litellm", model_name="claude-sonnet-5"),
        requested_provider="anthropic",
        requested_model="claude-sonnet-5",
    )
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-r3", metadata=TRACE)
    callback.on_llm_end(
        _response(provider="litellm", model="claude-sonnet-5"),
        run_id="call-r3",
    )
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    record = accounting.get_model_accounting_recorder().records()[0]

    assert record["routing"]["fallback_from"] is None


def test_actual_model_change_is_explicit_fallback_evidence():
    """A different model from the same provider is still a fallback."""
    callback = _instrument(
        FakeModel(provider="anthropic", model_name="claude-haiku-4-5"),
        requested_provider="anthropic",
        requested_model="claude-sonnet-5",
    )
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-r4", metadata=TRACE)
    callback.on_llm_end(
        _response(provider="anthropic", model="claude-haiku-4-5"),
        run_id="call-r4",
    )
    accounting.get_model_accounting_recorder().finalize_trace(
        "trace-123", status="success"
    )

    record = accounting.get_model_accounting_recorder().records()[0]

    assert record["routing"]["fallback_from"] == "anthropic/claude-sonnet-5"


def test_error_and_missing_trace_are_incomplete_without_error_text_leakage():
    callback = _instrument(FakeModel())
    callback.on_llm_start({}, ["prompt"], run_id="call-4")
    callback.on_llm_error(RuntimeError("provider secret detail"), run_id="call-4")

    record = accounting.get_model_accounting_recorder().records()[0]
    summary = accounting.get_model_accounting_recorder().summary()

    assert record["status"] == "error"
    assert record["error_type"] == "RuntimeError"
    assert "provider secret detail" not in json.dumps(record)
    assert summary["complete"] is False
    assert any(
        "root_trace_id_missing" in reason for reason in summary["completeness_reasons"]
    )


def test_no_calls_is_never_reported_as_complete_or_zero_cost():
    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="missing"
    )

    assert summary["complete"] is False
    assert summary["cost_usd"] is None
    assert summary["completeness_reasons"] == ["no_model_calls_recorded"]


def test_successful_calls_remain_incomplete_until_trace_finalization():
    callback = _instrument(FakeModel())
    callback.on_chat_model_start({}, [["prompt"]], run_id="call-5", metadata=TRACE)
    callback.on_llm_end(_response(), run_id="call-5")

    summary = accounting.get_model_accounting_recorder().summary(
        root_trace_id="trace-123"
    )

    assert summary["complete"] is False
    assert summary["cost_usd"] is None
    assert "trace-123:trace_not_finalized" in summary["completeness_reasons"]


def test_langchain_invocation_propagates_graph_trace_metadata():
    fake_models = pytest.importorskip("langchain_core.language_models.fake_chat_models")
    model = fake_models.FakeListChatModel(responses=["ok"])
    _instrument(model, requested_provider="fake", requested_model="fake-list")

    asyncio.run(model.ainvoke("hello", config={"metadata": TRACE}))

    record = accounting.get_model_accounting_recorder().records()[0]
    assert record["trace"] == TRACE
    assert record["routing"]["requested_provider"] == "fake"


def test_model_router_attaches_required_accounting(monkeypatch):
    try:
        from sre_agent import llm_utils, model_router
    except ImportError as exc:
        pytest.skip(f"full model runtime unavailable: {exc}")

    model = FakeModel()
    monkeypatch.setattr(
        llm_utils,
        "create_llm_with_error_handling",
        lambda provider, **kwargs: model,
    )
    monkeypatch.setenv("MODEL_ROUTER_ENABLED", "true")
    monkeypatch.delenv("MODEL_ROUTER_BACKEND", raising=False)
    monkeypatch.delenv("LITELLM_ENABLED", raising=False)

    routed = model_router.route_llm(
        model_router.TaskType.PLANNING,
        provider="anthropic",
        use_fallback=True,
    )

    assert routed is model
    assert any(
        type(callback).__name__ == "ModelAccountingCallback"
        for callback in model.callbacks
    )
    callback = next(
        callback
        for callback in model.callbacks
        if type(callback).__name__ == "ModelAccountingCallback"
    )
    assert callback.identity.fallback_allowed is False
