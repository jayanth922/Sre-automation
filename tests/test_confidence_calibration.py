#!/usr/bin/env python3
"""Tests for task-specific confidence calibration and drift detection."""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FINGERPRINT = "c" * 64
MODULE_PATH = ROOT / "sre_agent" / "confidence_calibration.py"
_spec = importlib.util.spec_from_file_location("confidence_calibration", MODULE_PATH)
calibration = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = calibration
_spec.loader.exec_module(calibration)


def _record(index, confidence, outcome, *, task="remediation"):
    return calibration.build_confidence_record(
        task=task,
        rubric_version="sre-structured-v1",
        raw_confidence=confidence,
        outcome=outcome,
        scenario="bad_deploy_checkout",
        scenario_version="1.0.0",
        dataset_sha256="d" * 64,
        config_fingerprint=CONFIG_FINGERPRINT,
        pair_id=f"pair-{index:04d}",
        observed_at=datetime(2026, 8, 26, tzinfo=timezone.utc)
        + timedelta(seconds=index),
    )


def _separable_records(count=200):
    midpoint = count // 2
    return tuple(
        _record(
            index,
            (
                0.1 + (0.3 * index / max(1, midpoint - 1))
                if index < midpoint
                else 0.8 + (0.19 * (index - midpoint) / max(1, midpoint - 1))
            ),
            index >= midpoint,
        )
        for index in range(count)
    )


def test_reliability_report_computes_proper_scores_and_bins():
    records = tuple(
        [
            _record(1, 0.1, False),
            _record(2, 0.2, False),
            _record(3, 0.8, True),
            _record(4, 0.9, True),
        ]
    )

    report = calibration.reliability_report(records, task="remediation", bin_count=5)

    assert report.samples == 4
    assert report.brier_score == pytest.approx(0.025)
    assert report.expected_calibration_error == pytest.approx(0.15)
    assert report.outcome_rate == 0.5
    assert sum(item.count for item in report.bins) == 4


def test_artifact_requires_real_sample_support():
    records = _separable_records(count=20)

    with pytest.raises(calibration.ConfidenceCalibrationError, match="requires"):
        calibration.build_calibration_artifact(
            records,
            task="remediation",
            source_sha256="a" * 64,
            config_fingerprint=CONFIG_FINGERPRINT,
            artifact_version="remediation-v1",
            minimum_samples=100,
        )


def test_artifact_requires_distinct_supported_confidence_bins():
    records = tuple(_record(index, 0.9, True) for index in range(120))

    with pytest.raises(
        calibration.ConfidenceCalibrationError,
        match="supported confidence bins",
    ):
        calibration.build_calibration_artifact(
            records,
            task="remediation",
            source_sha256="a" * 64,
            config_fingerprint=CONFIG_FINGERPRINT,
            artifact_version="remediation-v1",
        )


def test_artifact_is_monotonic_content_addressed_and_runtime_usable(tmp_path):
    records = _separable_records()
    artifact = calibration.build_calibration_artifact(
        records,
        task="remediation",
        source_sha256="a" * 64,
        config_fingerprint=CONFIG_FINGERPRINT,
        artifact_version="remediation-v1",
        minimum_samples=100,
        minimum_bin_samples=20,
        minimum_threshold_support=40,
        required_wilson_lower=0.9,
    )
    probabilities = [item.calibrated_probability for item in artifact.bins]

    assert probabilities == sorted(probabilities)
    assert artifact.autonomy_threshold is not None
    assert len(artifact.artifact_sha256) == 64

    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(path, artifact)
    loaded = calibration.load_calibration_artifact(path)
    high = calibration.calibrate_confidence(
        0.95,
        loaded,
        task="remediation",
        config_fingerprint=CONFIG_FINGERPRINT,
    )
    low = calibration.calibrate_confidence(
        0.15,
        loaded,
        task="remediation",
        config_fingerprint=CONFIG_FINGERPRINT,
    )

    assert loaded == artifact
    assert high.calibrated_probability >= low.calibrated_probability
    assert high.autonomy_eligible is True
    assert low.autonomy_eligible is False

    with pytest.raises(calibration.ConfidenceCalibrationError, match="configuration"):
        calibration.calibrate_confidence(
            0.95,
            loaded,
            task="remediation",
            config_fingerprint="e" * 64,
        )


def test_tampered_artifact_is_rejected(tmp_path):
    artifact = calibration.build_calibration_artifact(
        _separable_records(),
        task="remediation",
        source_sha256="a" * 64,
        config_fingerprint=CONFIG_FINGERPRINT,
        artifact_version="remediation-v1",
    )
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(path, artifact)
    payload = json.loads(path.read_text())
    payload["bins"][-1]["calibrated_probability"] = 0.1
    path.write_text(json.dumps(payload))

    with pytest.raises(calibration.ConfidenceCalibrationError, match="digest|invalid"):
        calibration.load_calibration_artifact(path)


def test_confidence_records_are_strict_and_content_addressed(tmp_path):
    path = tmp_path / "confidence.jsonl"
    record = _record(1, 0.8, True)
    calibration.append_confidence_record(path, record)

    loaded, digest = calibration.load_confidence_records(path)

    assert loaded == (record,)
    assert len(digest) == 64

    calibration.append_confidence_record(path, record)
    with pytest.raises(calibration.ConfidenceCalibrationError, match="duplicate"):
        calibration.load_confidence_records(path)


def test_drift_report_detects_worsening_calibration():
    reference = calibration.reliability_report(
        tuple(
            _record(index, 0.9 if index % 2 else 0.1, bool(index % 2))
            for index in range(40)
        ),
        task="remediation",
    )
    current = calibration.reliability_report(
        tuple(_record(index, 0.95, bool(index % 2)) for index in range(40, 80)),
        task="remediation",
    )

    drift = calibration.calibration_drift(reference, current)

    assert drift["status"] == "DRIFTED"
    assert drift["reasons"]
