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
    assert summary["tokens"] == {"input": 10, "output": 4, "total": 14}
    assert summary["root_trace_ids"] == ["trace-123"]
    assert record["routing"]["actual_provider"] == "groq"
    assert record["trace"] == TRACE
    assert [item["record_type"] for item in artifact] == [
        "model_call",
        "trace_finalization",
    ]
    assert "secret prompt" not in artifact_text


def test_missing_provider_cost_fails_closed_instead_of_estimating():
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
        provider="groq",
        use_fallback=False,
    )

    assert routed is model
    assert any(
        type(callback).__name__ == "ModelAccountingCallback"
        for callback in model.callbacks
    )
