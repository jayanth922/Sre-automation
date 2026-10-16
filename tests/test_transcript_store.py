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
from benchmarks import calibration_cases, evidence_locator, transcript_store
from langchain_core.messages import AIMessage, ToolMessage

from sre_agent.evidence_artifacts import encode_specialist_trace

RUN = "b6c331e9c0a5489c94fe5217d8e65e84"


def _export_row(canonical: bytes, root: str = "", created_at: str = "") -> str:
    digest = hashlib.sha256(canonical).hexdigest()
    payload = base64.b64encode(gzip.compress(canonical, mtime=0)).decode()
    if root or created_at:
        return f"{digest}|{root}|{created_at}|{payload}\n"
    return f"{digest}|{payload}\n"


def _transcript(
    agent_name: str = "metrics_agent",
    tool: str = "get_metric_range",
    args: dict = None,
    returned: str = "peak 0.0319",
) -> tuple[bytes, str]:
    canonical, _compressed, digest, _count = encode_specialist_trace(
        agent_name=agent_name,
        messages=[
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "private", "signature": "sig"},
                    {"type": "text", "text": "Checking the error ratio."},
                ],
                tool_calls=[
                    {"name": tool, "args": args or {"query": "ratio"}, "id": "t1"}
                ],
            ),
            ToolMessage(content=returned, name=tool, tool_call_id="t1"),
        ],
        raw_response="done",
        tool_failures=[],
    )
    return canonical, digest


def _grader_record(
    digest: str, root: str = "", internal: tuple = (), evidence: list = None
) -> dict:
    ref = {"sha256": digest, **({"root_trace_id": root} if root else {})}
    summary = {
        "benchmark_evaluation": {
            "schema_version": 1,
            "causal_chain": [{"cause": "declines", "effect": "low ratio"}],
            "evidence": evidence or [{"claim": "peaked at 3.19%", "source": "prometheus"}],
        }
    }
    if internal:
        summary["internal_evidence_sources"] = list(internal)
    raw_output = {
        "summary_text": "no action",
        "events": [
            {"event_type": "finding", "payload": {"evidence_artifact_ref": ref}},
            {"event_type": "summary", "payload": summary},
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


def _prescan_case(tmp_path, *, indexed: bool):
    """A metrics finding plus the Kubernetes prescan, which posts no finding."""
    metrics, metrics_digest = _transcript()
    prescan, prescan_digest = _transcript(
        "kubernetes_agent",
        "list_events",
        {"namespace": "meridian", "limit": 50},
        "Warning/BackOff",
    )
    other_run, _ = _transcript("kubernetes_agent", "list_pods", {"namespace": "other"})
    rows = [
        _export_row(metrics, RUN, "2026-09-21 15:52:10.5+00"),
        _export_row(prescan, RUN, "2026-09-21 15:51:02.1+00"),
        _export_row(other_run, "f" * 32, "2026-09-21 15:51:03+00"),
    ]
    if not indexed:
        rows = [row.split("|")[0] + "|" + row.split("|")[-1] for row in rows]
    store = tmp_path / "store"
    transcript_store.write_store(rows, store)
    record = _grader_record(
        metrics_digest,
        root=RUN,
        internal=("kubernetes_agent",),
        evidence=[
            {
                "source": "kubernetes",
                "reference": "list_events (namespace=meridian, limit=50) — Warning/BackOff",
                "claim": "the pod is backing off",
            }
        ],
    )
    case_set = calibration_cases.build_case_set(
        json.dumps(record).encode() + b"\n",
        blind_key=b"k" * 32,
        transcripts=calibration_cases.load_transcripts(store),
        trace_index=transcript_store.read_index(store),
    )
    return case_set, prescan_digest


def test_the_prescan_transcript_is_attached_by_its_run(tmp_path):
    case_set, _prescan_digest = _prescan_case(tmp_path, indexed=True)

    [case] = case_set.review_cases
    review_input = case["review_input"]
    sources = [t["source"] for t in review_input["specialist_transcripts"]]
    assert sources == ["metrics_agent", "kubernetes_agent"]
    assert review_input["unattached_sources"] == []
    assert review_input["missing_transcripts"] == 0
    assert (case_set.transcripts_attached, case_set.transcripts_by_run) == (2, 1)
    assert case_set.cases_with_unattached_sources == 0
    # The consumer: the cited prescan call is now found.
    assert evidence_locator.check_case(case, frozenset({"list_events"})).state == "PASS"


def test_without_an_index_the_prescan_is_named_unattached_and_nothing_fails(tmp_path):
    case_set, _prescan_digest = _prescan_case(tmp_path, indexed=False)

    [case] = case_set.review_cases
    assert case["review_input"]["unattached_sources"] == ["kubernetes_agent"]
    assert case_set.cases_with_unattached_sources == 1
    result = evidence_locator.check_case(case, frozenset({"list_events"}))
    assert result.state == "UNCHECKABLE"
    assert result.entries[0].detail.endswith("some transcripts are missing")


def test_the_run_index_merges_across_writes_and_rejects_corruption(tmp_path):
    first, first_digest = _transcript()
    second, second_digest = _transcript("logs_agent", "query_logs", {"logql": "{a=\"b\"}"})

    transcript_store.write_store([_export_row(first, RUN, "2026-09-21 15:51:00+00")], tmp_path)
    again = transcript_store.write_store(
        [_export_row(second, RUN, "2026-09-21 15:52:00+00"), _export_row(first)], tmp_path
    )

    assert again.indexed == 2
    assert [o.sha256 for o in transcript_store.read_index(tmp_path)] == [
        first_digest,
        second_digest,
    ]
    (tmp_path / transcript_store.INDEX_NAME).write_text("{not json\n")
    with pytest.raises(transcript_store.TranscriptStoreError, match="index.jsonl line 1"):
        transcript_store.write_store([_export_row(first, RUN, "x")], tmp_path)


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
