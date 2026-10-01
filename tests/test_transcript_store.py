"""Round trip: real specialist transcript -> psql export -> store -> review case.

`encode_specialist_trace` is the producer `persist_specialist_trace` uses, and
`calibration_cases` is the consumer, so a field renamed on either side fails
here instead of in a labeller's empty transcript.
"""

import base64
import gzip
import hashlib
import json

import pytest
from benchmarks import calibration_cases, transcript_store
from langchain_core.messages import AIMessage, ToolMessage

from sre_agent.evidence_artifacts import encode_specialist_trace


def _export_row(canonical: bytes) -> str:
    digest = hashlib.sha256(canonical).hexdigest()
    payload = base64.b64encode(gzip.compress(canonical, mtime=0)).decode()
    return f"{digest}|{payload}\n"


def _transcript() -> tuple[bytes, str]:
    canonical, _compressed, digest, _count = encode_specialist_trace(
        agent_name="metrics_agent",
        messages=[
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "private", "signature": "sig"},
                    {"type": "text", "text": "Checking the error ratio."},
                ],
                tool_calls=[
                    {"name": "get_metric_range", "args": {"query": "ratio"}, "id": "t1"}
                ],
            ),
            ToolMessage(content="peak 0.0319", name="get_metric_range", tool_call_id="t1"),
        ],
        raw_response="done",
        tool_failures=[],
    )
    return canonical, digest


def _grader_record(digest: str) -> dict:
    raw_output = {
        "summary_text": "no action",
        "events": [
            {
                "event_type": "finding",
                "payload": {"evidence_artifact_ref": {"sha256": digest}},
            },
            {
                "event_type": "summary",
                "payload": {
                    "benchmark_evaluation": {
                        "schema_version": 1,
                        "causal_chain": [{"cause": "declines", "effect": "low ratio"}],
                        "evidence": [{"claim": "peaked at 3.19%", "source": "prometheus"}],
                    }
                },
            },
        ],
    }
    encoded = json.dumps(raw_output, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "recorded_at": "2026-09-30T16:09:00+00:00",
        "scenario": "payment_subthreshold_charge_errors",
        "dataset_version": "sentinel-sre-v3",
        "scenario_version": "3.0.0",
        "oracle_status": "NO_ACTION_CORRECT",
        "application_status": "resolved",
        "raw_output_sha256": hashlib.sha256(encoded).hexdigest(),
        "raw_output": raw_output,
        "score": {
            "structured_grade": {
                "rubric_version": "sre-structured-v1",
                "rubric_sha256": calibration_cases.EXPECTED_RUBRIC_SHA256,
            }
        },
    }


def test_real_transcript_reaches_the_review_case(tmp_path):
    canonical, digest = _transcript()
    store = tmp_path / "store"

    result = transcript_store.write_store([_export_row(canonical)], store)
    cases = calibration_cases.build_case_set(
        json.dumps(_grader_record(digest)).encode() + b"\n",
        blind_key=b"k" * 32,
        transcripts=calibration_cases.load_transcripts(store),
    )

    assert result.written == 1
    [case] = cases.review_cases
    [transcript] = case["review_input"]["specialist_transcripts"]
    assistant, tool = transcript["turns"]
    assert transcript["source"] == "metrics_agent"
    assert assistant["text"] == "Checking the error ratio."
    assert assistant["tool_calls"] == [
        {"name": "get_metric_range", "args": {"query": "ratio"}}
    ]
    assert (tool["name"], tool["content"]) == ("get_metric_range", "peak 0.0319")
    assert "private" not in json.dumps(case)
    assert "payment_subthreshold" not in json.dumps(case)


def test_rewriting_the_store_is_idempotent(tmp_path):
    canonical, _digest = _transcript()

    transcript_store.write_store([_export_row(canonical)], tmp_path)
    again = transcript_store.write_store([_export_row(canonical)], tmp_path)

    assert (again.written, again.already_present) == (0, 1)


def test_a_row_that_is_not_its_digest_writes_nothing(tmp_path):
    canonical, _digest = _transcript()
    good = _export_row(canonical)
    forged = _export_row(b'{"forged":true}').split("|")[0] + "|" + good.split("|")[1]

    with pytest.raises(transcript_store.TranscriptStoreError, match="digest"):
        transcript_store.write_store([good, forged], tmp_path / "store")

    assert not (tmp_path / "store").exists()


def test_malformed_rows_are_refused(tmp_path):
    with pytest.raises(transcript_store.TranscriptStoreError, match="sha256"):
        transcript_store.write_store(["not-a-row\n"], tmp_path)
    with pytest.raises(transcript_store.TranscriptStoreError, match="no transcripts"):
        transcript_store.write_store(["\n"], tmp_path)
