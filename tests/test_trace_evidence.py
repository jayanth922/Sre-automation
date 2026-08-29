#!/usr/bin/env python3
"""A08 tests for complete, privacy-safe root-run trace evidence."""

import asyncio
import json
import stat

import pytest

from sre_agent import trace_evidence

TRACE = {
    "root_trace_id": "trace-123",
    "run_manifest_id": "manifest-123",
    "incident_id": "incident-123",
    "job_id": "job-123",
}
MODEL_ACCOUNTING = {
    "complete": True,
    "completeness_reasons": [],
    "cost_usd": 0.0125,
    "tokens": {"input": 10, "output": 4, "total": 14},
    "latency_ms": 25.0,
}


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_EVIDENCE_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.delenv("TRACE_PAYLOAD_CAPTURE", raising=False)
    monkeypatch.delenv("TRACE_PAYLOAD_MAX_CHARS", raising=False)
    trace_evidence.get_run_trace_recorder().reset()


def _start_and_record_required_spans(*, include_verification=True):
    recorder = trace_evidence.get_run_trace_recorder()
    recorder.start_run(**TRACE)
    spans = [
        (
            "model",
            {
                "gen_ai.provider.name": "groq",
                "gen_ai.request.model": "model-a",
                "gen_ai.response.model": "model-a",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 4,
                "sentinel.usage.total_tokens": 14,
                "sentinel.cost.usd": 0.0125,
            },
        ),
        ("retrieval", {"sentinel.retrieval.outcome": "no_matches"}),
        (
            "tool",
            {"gen_ai.tool.name": "prometheus_query", "gen_ai.tool.type": "extension"},
        ),
        ("policy", {"sentinel.policy.decision": "autonomous"}),
        ("approval", {"sentinel.approval.outcome": "not_required"}),
        (
            "mutation",
            {
                "sentinel.mutation.live_enabled": False,
                "sentinel.mutation.count": 0,
            },
        ),
    ]
    if include_verification:
        spans.append(("verification", {"sentinel.verification.outcome": "not_run"}))
    for kind, attributes in spans:
        recorder.record_span(
            root_trace_id=TRACE["root_trace_id"],
            span_kind=kind,
            name=f"{kind} span",
            status="not_applicable" if kind in {"approval", "mutation"} else "success",
            attributes=attributes,
        )
    return recorder


def test_complete_trace_requires_root_all_span_kinds_and_semantic_attributes():
    recorder = _start_and_record_required_spans()
    recorder.finish_run(TRACE["root_trace_id"], status="success")

    summary = recorder.summary(
        root_trace_id=TRACE["root_trace_id"],
        model_accounting=MODEL_ACCOUNTING,
    )
    root = recorder.records(TRACE["root_trace_id"])[0]

    assert summary["complete"] is True
    assert recorder.root_trace_ids(incident_id="incident-123") == ("trace-123",)
    assert summary["spans"] == 7
    assert summary["cost_usd"] == pytest.approx(0.0125)
    assert summary["tokens"] == {"input": 10, "output": 4, "total": 14}
    assert summary["observed_span_kinds"] == sorted(trace_evidence.REQUIRED_SPAN_KINDS)
    assert root["attributes"]["gen_ai.operation.name"] == "invoke_workflow"
    assert root["attributes"]["gen_ai.conversation.id"] == "incident-123"
    assert stat.S_IMODE(trace_evidence._artifact_path().stat().st_mode) == 0o600


def test_missing_span_or_required_attribute_fails_closed():
    recorder = _start_and_record_required_spans(include_verification=False)
    recorder.finish_run(TRACE["root_trace_id"], status="success")

    summary = recorder.summary(
        root_trace_id=TRACE["root_trace_id"],
        model_accounting=MODEL_ACCOUNTING,
    )

    assert summary["complete"] is False
    assert "missing_span:verification" in summary["completeness_reasons"]

    recorder.record_span(
        root_trace_id=TRACE["root_trace_id"],
        span_kind="verification",
        name="verification span",
        attributes={},
    )
    summary = recorder.summary(
        root_trace_id=TRACE["root_trace_id"],
        model_accounting=MODEL_ACCOUNTING,
    )
    assert (
        "missing_attribute:verification:sentinel.verification.outcome"
        in summary["completeness_reasons"]
    )


def test_error_spans_are_reconciled_without_hiding_a_complete_trace():
    recorder = _start_and_record_required_spans()
    recorder.record_span(
        root_trace_id=TRACE["root_trace_id"],
        span_kind="tool",
        name="failed optional tool",
        status="error",
        attributes={
            "gen_ai.tool.name": "optional_tool",
            "gen_ai.tool.type": "extension",
        },
    )
    recorder.finish_run(TRACE["root_trace_id"], status="success")

    summary = recorder.summary(
        root_trace_id=TRACE["root_trace_id"],
        model_accounting=MODEL_ACCOUNTING,
    )

    assert summary["complete"] is True
    assert summary["error_spans"] == 1


def test_payload_capture_is_off_by_default_and_redacted_when_opted_in(
    monkeypatch,
):
    recorder = trace_evidence.get_run_trace_recorder()
    recorder.start_run(**TRACE)
    secret = "password=hunter2 Bearer abcdefghijkl org_id=tenant-42"
    recorder.record_span(
        root_trace_id=TRACE["root_trace_id"],
        span_kind="retrieval",
        name="private retrieval",
        attributes={"sentinel.retrieval.outcome": "matches"},
        payload=secret,
    )
    assert recorder.records(TRACE["root_trace_id"])[-1]["payload"] is None

    monkeypatch.setenv("TRACE_PAYLOAD_CAPTURE", "true")
    monkeypatch.setenv("TRACE_PAYLOAD_MAX_CHARS", "64")
    recorder.record_span(
        root_trace_id=TRACE["root_trace_id"],
        span_kind="retrieval",
        name="private retrieval",
        attributes={"sentinel.retrieval.outcome": "matches"},
        payload=f"{secret} {'x' * 100}",
    )
    captured = recorder.records(TRACE["root_trace_id"])[-1]["payload"]

    assert "hunter2" not in captured
    assert "abcdefghijkl" not in captured
    assert "tenant-42" not in captured
    assert "[redacted" in captured
    assert captured.endswith("…[truncated]")


def test_tool_callback_records_correlated_metadata_without_payload():
    recorder = trace_evidence.get_run_trace_recorder()
    recorder.start_run(**TRACE)
    callback = trace_evidence.RunTraceCallback()

    callback.on_tool_start(
        {"name": "prometheus_query"},
        "password=do-not-store",
        run_id="tool-1",
        parent_run_id="parent-1",
        metadata=TRACE,
    )
    callback.on_tool_end("Bearer do-not-store-either", run_id="tool-1")

    record = recorder.records(TRACE["root_trace_id"])[-1]
    assert record["span_kind"] == "tool"
    assert record["span_id"] == "tool-1"
    assert record["parent_span_id"] == "parent-1"
    assert record["attributes"]["gen_ai.tool.name"] == "prometheus_query"
    assert record["payload"] is None
    assert "do-not-store" not in json.dumps(record)


def test_thread_config_attaches_local_trace_callback_for_root_trace(monkeypatch):
    from sre_agent import checkpointer

    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    config = checkpointer.thread_config(
        "incident-123", {"metadata": TRACE, "callbacks": []}
    )

    assert any(
        isinstance(callback, trace_evidence.RunTraceCallback)
        for callback in config["callbacks"]
    )


def test_incident_metrics_uses_latest_incident_root_even_without_prior_model_call(
    monkeypatch,
):
    from sre_agent import model_accounting, observability
    from sre_agent.api.v1 import mission_control

    class AccountingRecorder:
        def summary(self, **filters):
            return {
                "complete": False,
                "completeness_reasons": ["no_model_calls_recorded"],
                "root_filter": filters.get("root_trace_id"),
            }

    class TraceRecorder:
        def root_trace_ids(self, *, incident_id=None):
            assert incident_id == "incident-123"
            return ("trace-older", "trace-latest")

        def summary(self, *, root_trace_id, model_accounting):
            return {
                "complete": False,
                "root_trace_id": root_trace_id,
                "model_root_filter": model_accounting["root_filter"],
            }

    class NodeRecorder:
        def summary(self, incident_id):
            return {"incident_id": incident_id, "total_steps": 0}

    monkeypatch.setattr(
        model_accounting,
        "get_model_accounting_recorder",
        lambda: AccountingRecorder(),
    )
    monkeypatch.setattr(
        trace_evidence,
        "get_run_trace_recorder",
        lambda: TraceRecorder(),
    )
    monkeypatch.setattr(observability, "get_recorder", lambda: NodeRecorder())

    response = asyncio.run(
        mission_control.get_incident_agent_metrics(
            "incident-123", user=None, db=None, owned_incident=object()
        )
    )

    assert response["trace_completeness"] == {
        "complete": False,
        "root_trace_id": "trace-latest",
        "model_root_filter": "trace-latest",
    }
