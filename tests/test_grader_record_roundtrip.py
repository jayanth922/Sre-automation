#!/usr/bin/env python3
"""What the grader writes, the calibration builder has to be able to read.

`harness_approvals` was added to `append_grader_record` and never added to
`calibration_cases._RECORD_KEYS`, which compared key sets for exact equality.
Every grader record written afterwards -- both paid campaigns -- was rejected
as schema-invalid, so no calibration corpus could ever have been built from
real evidence. Nothing tested the producer against its own consumer.
"""

import json
from types import SimpleNamespace

import pytest

from benchmarks import calibration_cases, structured_grading


def _spec(expected_evidence=()):
    return SimpleNamespace(
        name="payment_provider_outage",
        dataset_version="sentinel-v2",
        scenario_version="2.0.0",
        ground_truth_service="payment-service",
        taxonomy={"fault_mode": "provider_outage"},
        expected_action_types={"escalate"},
        unsafe_action_types=set(),
        expected_severity_band={"SEV1"},
        expected_evidence=list(expected_evidence),
    )


def _events():
    return [
        {
            "event_type": "summary",
            "payload": {
                "benchmark_evaluation": {
                    "schema_version": 1,
                    "diagnosis": {
                        "service": "payment-service",
                        "fault_mode": "provider_outage",
                    },
                    "causal_chain": [{"cause": "provider", "effect": "charges fail"}],
                    "evidence": [
                        {
                            "claim": "payment_provider_up reported the dependency "
                            "unavailable",
                            "source": "prometheus",
                        }
                    ],
                }
            },
        }
    ]


def _write(tmp_path, spec, approvals=2):
    target = tmp_path / "grades.jsonl"
    score = SimpleNamespace(
        to_dict=lambda: {
            "structured_grade": {
                "rubric_version": structured_grading.EXPECTED_RUBRIC_VERSION,
                "rubric_sha256": calibration_cases.EXPECTED_RUBRIC_SHA256,
            }
        }
    )
    structured_grading.append_grader_record(
        target,
        spec=spec,
        oracle_status="VERIFIED_RECOVERED",
        application_status="resolved",
        summary_text="agent output",
        events=_events(),
        score=score,
        harness_approvals=approvals,
    )
    return target


def test_a_record_the_grader_writes_is_one_calibration_can_read(tmp_path):
    target = _write(tmp_path, _spec())

    records = calibration_cases._parse_records(target.read_bytes())

    assert len(records) == 1
    assert records[0]["harness_approvals"] == 2


def test_an_unknown_key_is_still_refused(tmp_path):
    target = _write(tmp_path, _spec())
    record = json.loads(target.read_text())
    record["invented_later"] = True

    with pytest.raises(calibration_cases.CalibrationCaseError, match="schema v1"):
        calibration_cases._parse_records(
            (json.dumps(record) + "\n").encode("utf-8")
        )


def test_a_missing_core_key_is_still_refused(tmp_path):
    target = _write(tmp_path, _spec())
    record = json.loads(target.read_text())
    del record["oracle_status"]

    with pytest.raises(calibration_cases.CalibrationCaseError, match="schema v1"):
        calibration_cases._parse_records(
            (json.dumps(record) + "\n").encode("utf-8")
        )


def test_the_judge_is_not_told_the_harness_approved(tmp_path):
    target = _write(tmp_path, _spec())

    case_set = calibration_cases.build_case_set(
        target.read_bytes(), blind_key=b"k" * 32
    )

    review = json.dumps(case_set.review_cases[0])
    assert "harness_approvals" not in review
    assert case_set.private_mapping[0]["harness_approvals"] == 2


def test_expected_evidence_is_read_at_last(tmp_path):
    spec = _spec(
        [
            "payment_provider_up reported the dependency unavailable",
            "checkout latency stayed below its rule threshold",
        ]
    )
    target = _write(tmp_path, spec)

    coverage = json.loads(target.read_text())["expected_evidence_coverage"]

    assert coverage["expected_count"] == 2
    assert coverage["matched_count"] == 1
    matched = {item["expectation"]: item["matched"] for item in coverage["items"]}
    assert matched["payment_provider_up reported the dependency unavailable"]
    assert not matched["checkout latency stayed below its rule threshold"]


def test_coverage_names_what_it_could_not_find(tmp_path):
    spec = _spec(["inventory p90 db_query_duration_seconds exceeded 1.0s"])
    target = _write(tmp_path, spec)

    item = json.loads(target.read_text())["expected_evidence_coverage"]["items"][0]

    assert not item["matched"]
    assert "db_query_duration_seconds" in item["missing_tokens"]


def test_coverage_does_not_claim_to_be_a_grade(tmp_path):
    spec = _spec(["nothing the agent ever said"])
    target = _write(tmp_path, spec)
    record = json.loads(target.read_text())

    assert record["expected_evidence_coverage"]["graded"] is False
    assert record["expected_evidence_coverage"]["matched_count"] == 0
    # A total miss is recorded, never escalated: the grade is untouched.
    assert record["score"]["structured_grade"]["rubric_version"] == (
        structured_grading.EXPECTED_RUBRIC_VERSION
    )


def test_a_scenario_with_no_expectations_records_an_empty_coverage(tmp_path):
    target = _write(tmp_path, _spec())

    coverage = json.loads(target.read_text())["expected_evidence_coverage"]

    assert coverage["expected_count"] == 0
    assert coverage["items"] == []
