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
