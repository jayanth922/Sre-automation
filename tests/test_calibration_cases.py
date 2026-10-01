import hashlib
import json

import pytest
from benchmarks import calibration_cases


def _record(scenario: str, output: str) -> dict:
    events = [
        {
            "event_type": "summary",
            "payload": {
                "benchmark_evaluation": {
                    "schema_version": 1,
                    "diagnosis": {"service": "checkout", "fault_mode": "latency"},
                    "causal_chain": [{"cause": "load", "effect": "latency"}],
                    "evidence": [{"claim": "p95 rose", "source": "metrics"}],
                }
            },
        }
    ]
    raw_output = {"summary_text": output, "events": events}
    encoded = json.dumps(raw_output, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "recorded_at": "2026-09-20T00:00:00+00:00",
        "scenario": scenario,
        "dataset_version": "sentinel-v2",
        "scenario_version": "2.0.0",
        "oracle_status": "UNRESOLVED",
        "application_status": "investigating",
        "raw_output_sha256": hashlib.sha256(encoded).hexdigest(),
        "raw_output": raw_output,
        "score": {
            "structured_grade": {
                "rubric_version": "sre-structured-v1",
                "rubric_sha256": calibration_cases.EXPECTED_RUBRIC_SHA256,
            }
        },
    }


def _raw(*records: dict) -> bytes:
    return b"".join(
        json.dumps(record, sort_keys=True).encode() + b"\n" for record in records
    )


def test_review_cases_are_blinded_but_private_mapping_preserves_provenance():
    cases = calibration_cases.build_case_set(
        _raw(_record("secret-scenario", "diagnosis text")),
        blind_key=b"k" * 32,
    )

    review = cases.review_cases[0]
    mapping = cases.private_mapping[0]
    assert "secret-scenario" not in json.dumps(review)
    assert "source_output_sha256" not in review
    assert review["blind_case_id"].startswith("case-")
    assert review["review_input"]["benchmark_evaluation"]["causal_chain"]
    assert mapping["scenario"] == "secret-scenario"
    assert mapping["blind_case_id"] == review["blind_case_id"]


def test_keyed_selection_is_reproducible_and_key_specific():
    raw = _raw(_record("a", "first"), _record("b", "second"))

    first = calibration_cases.build_case_set(raw, blind_key=b"a" * 32, limit=1)
    again = calibration_cases.build_case_set(raw, blind_key=b"a" * 32, limit=1)
    other = calibration_cases.build_case_set(raw, blind_key=b"b" * 32, limit=1)

    assert first == again
    assert (
        first.review_cases[0]["blind_case_id"] != other.review_cases[0]["blind_case_id"]
    )


def test_missing_structured_evaluation_fails_closed():
    record = _record("a", "output")
    record["raw_output"]["events"] = []
    encoded = json.dumps(
        record["raw_output"], sort_keys=True, separators=(",", ":")
    ).encode()
    record["raw_output_sha256"] = hashlib.sha256(encoded).hexdigest()

    with pytest.raises(
        calibration_cases.CalibrationCaseError,
        match="no structured benchmark evaluation",
    ):
        calibration_cases.build_case_set(_raw(record), blind_key=b"k" * 32)


def test_duplicate_output_cannot_be_counted_twice():
    record = _record("a", "same")

    with pytest.raises(calibration_cases.CalibrationCaseError, match="twice"):
        calibration_cases.build_case_set(
            _raw(record, {**record, "scenario": "b"}),
            blind_key=b"k" * 32,
        )


def test_tampered_output_digest_fails_closed():
    record = _record("a", "original")
    record["raw_output"]["summary_text"] = "tampered"

    with pytest.raises(calibration_cases.CalibrationCaseError, match="does not match"):
        calibration_cases.build_case_set(_raw(record), blind_key=b"k" * 32)


def test_stale_rubric_digest_fails_closed():
    record = _record("a", "output")
    record["score"]["structured_grade"]["rubric_sha256"] = "0" * 64

    with pytest.raises(calibration_cases.CalibrationCaseError, match="rubric digest"):
        calibration_cases.build_case_set(_raw(record), blind_key=b"k" * 32)


def _unreviewable(scenario: str, output: str) -> dict:
    """A record the grader really wrote: real output, no structured block."""
    record = _record(scenario, output)
    record["raw_output"]["events"] = [{"event_type": "step", "payload": {}}]
    encoded = json.dumps(
        record["raw_output"], sort_keys=True, separators=(",", ":")
    ).encode()
    record["raw_output_sha256"] = hashlib.sha256(encoded).hexdigest()
    return record


def test_one_unreviewable_record_does_not_discard_the_others():
    stale = _record("stale", "graded on an older rubric")
    stale["score"]["structured_grade"]["rubric_sha256"] = "0" * 64

    cases = calibration_cases.build_case_set(
        _raw(_unreviewable("bare", "no structured block"), stale, _record("ok", "out")),
        blind_key=b"k" * 32,
    )

    assert len(cases.review_cases) == 1
    assert cases.eligible_count == 1
    assert {entry.scenario for entry in cases.skipped} == {"bare", "stale"}
    assert {entry.line_number for entry in cases.skipped} == {1, 2}
    assert calibration_cases.skip_reason_counts(cases.skipped) == {
        "no structured benchmark evaluation": 1,
        "is not pinned to the current sre-structured-v1 rubric digest": 1,
    }


def test_corrupt_evidence_still_stops_the_whole_build():
    tampered = _record("bad", "original")
    tampered["raw_output"]["summary_text"] = "tampered"

    with pytest.raises(calibration_cases.CalibrationCaseError, match="does not match"):
        calibration_cases.build_case_set(
            _raw(_record("good", "out"), tampered), blind_key=b"k" * 32
        )


def test_a_file_with_nothing_reviewable_still_fails_closed():
    with pytest.raises(
        calibration_cases.CalibrationCaseError, match="no grader record is reviewable"
    ):
        calibration_cases.build_case_set(
            _raw(_unreviewable("a", "one"), _unreviewable("b", "two")),
            blind_key=b"k" * 32,
        )


def test_manifest_reports_what_was_left_out(tmp_path):
    cases = calibration_cases.build_case_set(
        _raw(_unreviewable("bare", "skip me"), _record("ok", "out")),
        blind_key=b"k" * 32,
    )
    manifest = tmp_path / "manifest.json"

    calibration_cases.write_case_set(
        cases,
        review_path=tmp_path / "review.jsonl",
        mapping_path=tmp_path / "mapping.jsonl",
        manifest_path=manifest,
    )

    payload = json.loads(manifest.read_text())
    assert payload["cases"] == 1
    assert payload["eligible_records"] == 1
    assert payload["skipped_records"] == 1
    assert payload["skipped_reasons"] == {"no structured benchmark evaluation": 1}


def test_written_manifest_content_addresses_both_outputs(tmp_path):
    cases = calibration_cases.build_case_set(
        _raw(_record("a", "output")), blind_key=b"k" * 32
    )
    review = tmp_path / "review.jsonl"
    mapping = tmp_path / "mapping.jsonl"
    manifest = tmp_path / "manifest.json"

    calibration_cases.write_case_set(
        cases,
        review_path=review,
        mapping_path=mapping,
        manifest_path=manifest,
    )

    payload = json.loads(manifest.read_text())
    assert payload["cases"] == 1
    assert payload["review_sha256"] == hashlib.sha256(review.read_bytes()).hexdigest()
    assert (
        payload["private_mapping_sha256"]
        == hashlib.sha256(mapping.read_bytes()).hexdigest()
    )


def _empty_output(scenario: str) -> dict:
    """What `_score_without_output` writes for a run that produced nothing."""
    record = _record(scenario, "")
    record["raw_output"] = {"summary_text": "", "events": []}
    encoded = json.dumps(
        record["raw_output"], sort_keys=True, separators=(",", ":")
    ).encode()
    record["raw_output_sha256"] = hashlib.sha256(encoded).hexdigest()
    return record


def test_identical_empty_outputs_do_not_abort_the_file():
    # 2026-09-19: four INVALID_SCENARIO rows with no output shared one digest,
    # and the duplicate check rejected reports/sre-bench-grades.jsonl outright.
    cases = calibration_cases.build_case_set(
        _raw(_empty_output("x"), _empty_output("y"), _record("ok", "out")),
        blind_key=b"k" * 32,
    )

    assert len(cases.review_cases) == 1
    assert calibration_cases.skip_reason_counts(cases.skipped) == {
        "no structured benchmark evaluation": 2
    }


def test_a_run_copied_into_two_files_is_reviewed_once():
    shared = _record("shared", "out")
    first, second = _raw(shared), _raw(_record("other", "else"), shared)

    cases = calibration_cases.build_case_set([first, second], blind_key=b"k" * 32)

    assert len(cases.review_cases) == 2
    [skip] = cases.skipped
    assert (skip.source_index, skip.line_number) == (1, 2)
    assert skip.reason == "already taken from an earlier input file"
    assert cases.input_sha256s == (
        hashlib.sha256(first).hexdigest(),
        hashlib.sha256(second).hexdigest(),
    )


def test_a_single_file_keeps_its_input_digest():
    raw = _raw(_record("a", "out"))

    cases = calibration_cases.build_case_set(raw, blind_key=b"k" * 32)

    assert cases.input_sha256 == hashlib.sha256(raw).hexdigest()
    assert cases.transcripts_attached is None


def _with_finding(record: dict, digest: str) -> dict:
    record["raw_output"]["events"].insert(
        0,
        {
            "event_type": "finding",
            "payload": {"evidence_artifact_ref": {"sha256": digest}},
        },
    )
    encoded = json.dumps(
        record["raw_output"], sort_keys=True, separators=(",", ":")
    ).encode()
    record["raw_output_sha256"] = hashlib.sha256(encoded).hexdigest()
    return record


def _canonical(text: str) -> bytes:
    return json.dumps(
        {
            "messages": [{"type": "ToolMessage", "data": {"name": "q", "content": text}}],
            "source": "metrics_agent",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_missing_transcripts_are_counted_not_fatal():
    present, absent = _canonical("0.0319"), _canonical("never exported")
    present_sha = hashlib.sha256(present).hexdigest()
    absent_sha = hashlib.sha256(absent).hexdigest()
    raw = _raw(
        _with_finding(_record("a", "one"), present_sha),
        _with_finding(_record("b", "two"), absent_sha),
        _record("c", "legacy run without durable transcripts"),
    )

    cases = calibration_cases.build_case_set(
        raw, blind_key=b"k" * 32, transcripts={present_sha: present}
    )

    assert (
        cases.transcripts_attached,
        cases.transcripts_missing,
        cases.cases_without_transcripts,
    ) == (1, 1, 1)
    attached = [
        case["review_input"]
        for case in cases.review_cases
        if case["review_input"]["specialist_transcripts"]
    ]
    assert attached[0]["specialist_transcripts"][0]["turns"][0]["content"] == "0.0319"


def test_a_transcript_that_is_not_its_digest_stops_the_build():
    digest = hashlib.sha256(_canonical("real")).hexdigest()

    with pytest.raises(calibration_cases.CalibrationCaseError, match="digest"):
        calibration_cases.build_case_set(
            _raw(_with_finding(_record("a", "out"), digest)),
            blind_key=b"k" * 32,
            transcripts={digest: _canonical("swapped")},
        )


def test_transcript_store_rejects_a_misnamed_file(tmp_path):
    (tmp_path / f"{'0' * 64}.json").write_bytes(_canonical("x"))

    with pytest.raises(calibration_cases.CalibrationCaseError, match="digest"):
        calibration_cases.load_transcripts(tmp_path)


def test_manifest_records_transcript_coverage(tmp_path):
    cases = calibration_cases.build_case_set(
        _raw(_record("a", "out")), blind_key=b"k" * 32, transcripts={}
    )
    manifest = tmp_path / "manifest.json"

    calibration_cases.write_case_set(
        cases,
        review_path=tmp_path / "review.jsonl",
        mapping_path=tmp_path / "mapping.jsonl",
        manifest_path=manifest,
    )

    payload = json.loads(manifest.read_text())
    assert payload["cases_without_transcripts"] == 1
    assert payload["transcripts_attached"] == 0
    assert payload["input_sha256s"] == [payload["input_sha256"]]
