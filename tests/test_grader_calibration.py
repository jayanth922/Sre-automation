#!/usr/bin/env python3
"""Tests for blinded human-label agreement gates."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
_MODULE_PATH = BENCHMARKS / "grader_calibration.py"
_spec = importlib.util.spec_from_file_location("grader_calibration", _MODULE_PATH)
calibration = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = calibration
_spec.loader.exec_module(calibration)


def _label(case_id, labeler, causal, evidence):
    return {
        "schema_version": 1,
        "rubric_version": "sre-structured-v1",
        "blind_case_id": case_id,
        "labeler_id": labeler,
        "labeled_at": "2026-08-26T20:00:00+00:00",
        "criterion_labels": {
            "causal_chain": causal,
            "evidence_support": evidence,
        },
        "rationales": {
            "causal_chain": f"{causal.lower()} causal rationale",
            "evidence_support": f"{evidence.lower()} evidence rationale",
        },
    }


def _write(path, records):
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n"
    )


def test_perfect_mixed_labels_produce_kappa_one(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            _label("opaque-1", "rater-a", "PASS", "FAIL"),
            _label("opaque-1", "rater-b", "PASS", "FAIL"),
            _label("opaque-2", "rater-a", "FAIL", "PASS"),
            _label("opaque-2", "rater-b", "FAIL", "PASS"),
        ],
    )

    labels = calibration.load_human_labels(path)
    report = calibration.measure_agreement(labels)

    assert report.cases == 2
    assert report.labelers == 2
    assert report.criteria["causal_chain"].cohen_kappa == pytest.approx(1.0)
    assert report.criteria["evidence_support"].cohen_kappa == pytest.approx(1.0)
    calibration.require_release_ready(report, minimum_cases=2, minimum_kappa=0.8)


def test_release_gate_rejects_low_agreement(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            _label("opaque-1", "rater-a", "PASS", "PASS"),
            _label("opaque-1", "rater-b", "FAIL", "FAIL"),
            _label("opaque-2", "rater-a", "FAIL", "PASS"),
            _label("opaque-2", "rater-b", "PASS", "FAIL"),
        ],
    )
    report = calibration.measure_agreement(calibration.load_human_labels(path))

    with pytest.raises(calibration.CalibrationError, match="below"):
        calibration.require_release_ready(report, minimum_cases=2, minimum_kappa=0.6)


def test_every_case_requires_two_independent_labelers(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(path, [_label("opaque-1", "rater-a", "PASS", "PASS")])

    with pytest.raises(calibration.CalibrationError, match="two independent"):
        calibration.measure_agreement(calibration.load_human_labels(path))


def test_duplicate_labeler_case_pair_is_rejected(tmp_path):
    path = tmp_path / "labels.jsonl"
    record = _label("opaque-1", "rater-a", "PASS", "PASS")
    _write(path, [record, record])

    with pytest.raises(calibration.CalibrationError, match="only once"):
        calibration.load_human_labels(path)


def test_all_cases_require_the_same_two_raters(tmp_path):
    path = tmp_path / "labels.jsonl"
    _write(
        path,
        [
            _label("opaque-1", "rater-a", "PASS", "PASS"),
            _label("opaque-1", "rater-b", "PASS", "PASS"),
            _label("opaque-2", "rater-a", "FAIL", "FAIL"),
            _label("opaque-2", "rater-c", "FAIL", "FAIL"),
        ],
    )

    with pytest.raises(calibration.CalibrationError, match="same two"):
        calibration.measure_agreement(calibration.load_human_labels(path))


def test_labels_cannot_include_unblinded_extra_fields(tmp_path):
    path = tmp_path / "labels.jsonl"
    record = _label("opaque-1", "rater-a", "PASS", "PASS")
    record["scenario_id"] = "bad_deploy_checkout"
    _write(path, [record])

    with pytest.raises(calibration.CalibrationError, match="keys"):
        calibration.load_human_labels(path)
