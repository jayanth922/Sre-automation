#!/usr/bin/env python3
"""Tests for task-specific confidence calibration and drift detection."""

import hashlib
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


def _record(
    index,
    confidence,
    outcome,
    *,
    task="remediation",
    evidence_source="live_benchmark",
):
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
        evidence_source=evidence_source,
    )


def _graded_records(tiers, per_tier=60, evidence_source="live_benchmark"):
    """A corpus whose success rate genuinely rises with confidence.

    ``_separable_records`` collapses to two bins, which cannot show a cost
    model choosing between operating points.
    """
    records = []
    index = 0
    for base, rate in tiers:
        successes = round(per_tier * rate)
        for offset in range(per_tier):
            records.append(
                _record(
                    index,
                    base + 0.0005 * offset,
                    offset < successes,
                    evidence_source=evidence_source,
                )
            )
            index += 1
    return tuple(records)


_RISING = ((0.10, 0.30), (0.35, 0.60), (0.55, 0.80), (0.75, 0.93), (0.92, 1.00))


def _artifact(records=None, **overrides):
    options = {
        "task": "remediation",
        "source_sha256": "a" * 64,
        "config_fingerprint": CONFIG_FINGERPRINT,
        "artifact_version": "remediation-v1",
        # The graded corpus exists to show the cost model choosing between
        # operating points, so the Wilson floor is relaxed enough to leave it
        # more than one eligible choice.
        "required_wilson_lower": 0.50,
    }
    options.update(overrides)
    return calibration.build_calibration_artifact(
        _graded_records(_RISING) if records is None else records, **options
    )


def _retamper(path, mutate):
    """Edit an artifact and re-seal it, so only real validation can reject it."""
    payload = json.loads(path.read_text())
    mutate(payload)
    canonical = {
        key: value for key, value in payload.items() if key != "artifact_sha256"
    }
    payload["artifact_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(payload))


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


def test_threshold_curve_covers_every_bin_and_trades_coverage_for_safety():
    artifact = _artifact()
    curve = artifact.threshold_curve

    assert [point.threshold for point in curve] == sorted(
        {item.calibrated_probability for item in artifact.bins}
    )
    coverage = [point.coverage for point in curve]
    assert coverage == sorted(coverage, reverse=True)
    assert curve[0].coverage == 1.0
    assert curve[0].autonomous == artifact.sample_count
    for point in curve:
        assert point.autonomous + point.abstained == artifact.sample_count
        assert point.true_autonomy + point.false_autonomy == point.autonomous
        assert point.wilson_lower <= point.autonomous_success_rate


def test_cost_model_moves_the_threshold_rather_than_decorating_it():
    cheap = _artifact(false_autonomy_cost=2.0, abstention_cost=1.0)
    expensive = _artifact(false_autonomy_cost=200.0, abstention_cost=1.0)

    assert cheap.autonomy_threshold is not None
    assert expensive.autonomy_threshold is not None
    assert expensive.autonomy_threshold > cheap.autonomy_threshold

    def coverage_at(artifact):
        return next(
            point.coverage
            for point in artifact.threshold_curve
            if point.threshold == artifact.autonomy_threshold
        )

    # Paying more for a wrong autonomous action buys less autonomy.
    assert coverage_at(expensive) < coverage_at(cheap)
    # Identical bins; only the declared cost differs.
    assert cheap.bins == expensive.bins


def test_selected_point_is_the_cheapest_eligible_one():
    artifact = _artifact()
    eligible = [point for point in artifact.threshold_curve if point.eligible]

    assert eligible
    assert artifact.selected_cost == min(
        point.expected_cost_per_action for point in eligible
    )
    assert artifact.selected_cost <= artifact.always_abstain_cost
    assert artifact.selected_cost <= artifact.always_autonomous_cost
    assert artifact.autonomy_beats_abstention is True


def test_autonomy_can_lose_to_abstention_and_say_so():
    # No tier is reliable enough to be free, so once a wrong action costs two
    # hundred approvals the cheapest operating point is still worse than
    # sending everything to a human. The artifact reports that honestly rather
    # than presenting its threshold as a win.
    fallible = ((0.10, 0.30), (0.35, 0.60), (0.55, 0.80), (0.75, 0.90), (0.92, 0.95))
    artifact = _artifact(_graded_records(fallible), false_autonomy_cost=200.0)

    assert artifact.autonomy_threshold is not None
    assert artifact.selected_cost > artifact.always_abstain_cost
    assert artifact.autonomy_beats_abstention is False


def test_synthetic_evidence_yields_a_curve_but_never_a_threshold():
    artifact = _artifact(_graded_records(_RISING, evidence_source="synthetic"))

    assert artifact.threshold_curve
    assert artifact.autonomy_threshold is None
    assert artifact.threshold_support == 0
    assert artifact.threshold_wilson_lower is None
    assert artifact.selected_cost is None
    assert artifact.evidence_sources == ("synthetic",)
    assert "live benchmark" in artifact.autonomy_blocked_reason


def test_mixed_evidence_blocks_autonomy_even_when_mostly_live():
    live = _graded_records(_RISING)
    tainted = live[:-1] + (
        calibration.build_confidence_record(
            task="remediation",
            rubric_version="sre-structured-v1",
            raw_confidence=0.99,
            outcome=True,
            scenario="bad_deploy_checkout",
            scenario_version="1.0.0",
            dataset_sha256="d" * 64,
            config_fingerprint=CONFIG_FINGERPRINT,
            pair_id="pair-replay",
            observed_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
            evidence_source="replay",
        ),
    )

    artifact = _artifact(tainted)

    assert artifact.evidence_sources == ("live_benchmark", "replay")
    assert artifact.autonomy_threshold is None
    assert "replay" in artifact.autonomy_blocked_reason


def test_record_rejects_an_unknown_evidence_source():
    with pytest.raises(calibration.ConfidenceCalibrationError, match="evidence source"):
        _record(1, 0.9, True, evidence_source="vibes")


def test_recomputed_curve_rejects_a_resealed_hand_edit(tmp_path):
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(path, _artifact())
    _retamper(
        path,
        lambda payload: payload["threshold_curve"][0].update(
            {"false_autonomy": 0, "autonomous_success_rate": 1.0, "wilson_lower": 0.99}
        ),
    )

    with pytest.raises(
        calibration.ConfidenceCalibrationError, match="threshold curve does not match"
    ):
        calibration.load_calibration_artifact(path)


def test_resealed_synthetic_artifact_cannot_borrow_live_threshold_evidence(tmp_path):
    live = _artifact()
    synthetic_path = tmp_path / "synthetic.json"
    calibration.save_calibration_artifact(
        synthetic_path,
        _artifact(_graded_records(_RISING, evidence_source="synthetic")),
    )
    _retamper(
        synthetic_path,
        lambda payload: payload.update(
            {
                "autonomy_threshold": live.autonomy_threshold,
                "threshold_support": live.threshold_support,
                "threshold_wilson_lower": live.threshold_wilson_lower,
                "selected_cost": live.selected_cost,
                "autonomy_blocked_reason": None,
            }
        ),
    )

    with pytest.raises(calibration.ConfidenceCalibrationError, match="live benchmark"):
        calibration.load_calibration_artifact(synthetic_path)


def test_artifact_without_a_threshold_must_say_why(tmp_path):
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(
        path, _artifact(_graded_records(_RISING, evidence_source="synthetic"))
    )
    _retamper(path, lambda payload: payload.update({"autonomy_blocked_reason": None}))

    with pytest.raises(
        calibration.ConfidenceCalibrationError, match="grants no autonomy must say why"
    ):
        calibration.load_calibration_artifact(path)


def test_selection_rule_is_recorded_and_enforced(tmp_path):
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(path, _artifact())
    assert json.loads(path.read_text())["selection_rule"] == calibration.SELECTION_RULE

    _retamper(
        path,
        lambda payload: payload.update({"selection_rule": "whatever feels right"}),
    )
    with pytest.raises(
        calibration.ConfidenceCalibrationError, match="different selection rule"
    ):
        calibration.load_calibration_artifact(path)


def test_threshold_must_be_the_point_the_rule_selects(tmp_path):
    artifact = _artifact()
    other = next(
        point
        for point in artifact.threshold_curve
        if point.eligible and point.threshold != artifact.autonomy_threshold
    )
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(path, artifact)
    _retamper(
        path,
        lambda payload: payload.update(
            {
                "autonomy_threshold": other.threshold,
                "threshold_support": other.autonomous,
                "threshold_wilson_lower": other.wilson_lower,
                "selected_cost": other.expected_cost_per_action,
            }
        ),
    )

    with pytest.raises(
        calibration.ConfidenceCalibrationError, match="selection rule|threshold"
    ):
        calibration.load_calibration_artifact(path)


def test_calibrate_confidence_reports_why_autonomy_is_blocked(tmp_path):
    path = tmp_path / "artifact.json"
    calibration.save_calibration_artifact(
        path, _artifact(_graded_records(_RISING, evidence_source="synthetic"))
    )
    loaded = calibration.load_calibration_artifact(path)

    calibrated = calibration.calibrate_confidence(
        0.99,
        loaded,
        task="remediation",
        config_fingerprint=CONFIG_FINGERPRINT,
    )

    assert calibrated.autonomy_eligible is False
    assert "live benchmark" in calibrated.autonomy_blocked_reason
