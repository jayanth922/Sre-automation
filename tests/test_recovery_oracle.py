#!/usr/bin/env python3
"""Tests for the benchmark-owned recovery oracle."""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "recovery_oracle.py"
_spec = importlib.util.spec_from_file_location("recovery_oracle", _MODULE_PATH)
oracle = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = oracle
_spec.loader.exec_module(oracle)


def _probe(**overrides):
    values = {
        "name": "checkout_error_rate",
        "query": 'sum(rate(http_errors_total{service="checkout-service"}[5m]))',
        "operator": "lt",
        "threshold": 0.05,
        "unit": "ratio",
        "required_consecutive_passes": 2,
        "require_failure_observation": True,
    }
    values.update(overrides)
    return oracle.RecoveryProbe(**values)


def _tracker(started, *, baseline=0.01):
    tracker = oracle.RecoveryOracleTracker(_probe(), started)
    tracker.establish_baseline(baseline, observed_at=started - timedelta(seconds=1))
    return tracker


def test_probe_rejects_ambiguous_or_unsafe_definitions():
    with pytest.raises(ValueError, match="operator"):
        _probe(operator="contains")
    with pytest.raises(ValueError, match="required_consecutive_passes"):
        _probe(required_consecutive_passes=0)
    with pytest.raises(ValueError, match="query"):
        _probe(query=" ")


@pytest.mark.parametrize(
    ("operator", "value", "threshold", "passing"),
    [
        ("lt", 0.04, 0.05, True),
        ("lte", 0.05, 0.05, True),
        ("gt", 1.1, 1.0, True),
        ("gte", 1.0, 1.0, True),
        ("eq", 1.0, 1.0, True),
        ("eq", 0.0, 1.0, False),
    ],
)
def test_probe_comparators(operator, value, threshold, passing):
    assert _probe(operator=operator, threshold=threshold).passes(value) is passing


def test_passing_signal_cannot_resolve_until_fault_was_observed():
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started)

    first = tracker.observe(0.01, observed_at=started + timedelta(seconds=1))
    second = tracker.observe(0.01, observed_at=started + timedelta(seconds=2))

    assert first.state == second.state == "passing"
    assert tracker.recovered_at is None
    result = tracker.result(
        scenario="bad_deploy_checkout",
        incident_id="incident-1",
        application_status="resolved",
    )
    assert result.status == "INVALID_SCENARIO"
    assert result.false_resolved is True


def test_recovery_requires_consecutive_passes_after_failure():
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started)

    tracker.observe(0.40, observed_at=started + timedelta(seconds=3))
    tracker.observe(0.01, observed_at=started + timedelta(seconds=8))
    assert tracker.recovered_at is None
    tracker.observe(0.02, observed_at=started + timedelta(seconds=13))

    result = tracker.result(
        scenario="bad_deploy_checkout",
        incident_id="incident-1",
        application_status="investigated",
    )
    assert result.status == "VERIFIED_RECOVERED"
    assert result.mttr_seconds == pytest.approx(13.0)
    assert result.false_resolved is False
    assert result.failure_observed is True


def test_unknown_observation_resets_consecutive_passes():
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started)
    tracker.observe(0.40, observed_at=started + timedelta(seconds=1))
    tracker.observe(0.01, observed_at=started + timedelta(seconds=2))
    tracker.observe(None, observed_at=started + timedelta(seconds=3), error="timeout")
    tracker.observe(0.01, observed_at=started + timedelta(seconds=4))

    assert tracker.recovered_at is None
    assert tracker.observations[3].state == "unknown"


def test_application_resolution_is_recorded_as_false_when_oracle_fails():
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started)
    tracker.observe(0.40, observed_at=started + timedelta(seconds=1))

    result = tracker.result(
        scenario="bad_deploy_checkout",
        incident_id="incident-1",
        application_status="resolved",
    )

    assert result.status == "UNRESOLVED"
    assert result.false_resolved is True
    assert result.mttr_seconds is None


def test_unhealthy_baseline_cannot_be_relabelled_as_injected_recovery():
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started, baseline=0.40)
    tracker.observe(0.40, observed_at=started + timedelta(seconds=1))
    tracker.observe(0.01, observed_at=started + timedelta(seconds=2))
    tracker.observe(0.01, observed_at=started + timedelta(seconds=3))

    result = tracker.result(
        scenario="bad_deploy_checkout",
        incident_id="incident-1",
        application_status="resolved",
    )
    assert result.status == "INVALID_SCENARIO"
    assert result.baseline_healthy is False
    assert result.failure_observed is False
    assert result.recovered_at is None


def test_parse_prometheus_value_accepts_one_finite_scalar():
    vector = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1234, "0.037"]}],
        },
    }
    scalar = {
        "status": "success",
        "data": {"resultType": "scalar", "result": [1234, "2.5"]},
    }

    assert oracle.parse_prometheus_value(vector) == pytest.approx(0.037)
    assert oracle.parse_prometheus_value(scalar) == pytest.approx(2.5)


def test_parse_prometheus_value_fails_closed_on_ambiguous_or_bad_data():
    ambiguous = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"pod": "a"}, "value": [1, "1"]},
                {"metric": {"pod": "b"}, "value": [1, "2"]},
            ],
        },
    }
    failed = {"status": "error", "error": "query failed"}

    with pytest.raises(oracle.OracleQueryError, match="exactly one"):
        oracle.parse_prometheus_value(ambiguous)
    with pytest.raises(oracle.OracleQueryError, match="query failed"):
        oracle.parse_prometheus_value(failed)


def test_oracle_evidence_is_appended_as_separate_jsonl(tmp_path):
    started = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    tracker = _tracker(started)
    tracker.observe(0.40, observed_at=started + timedelta(seconds=1))
    result = tracker.result(
        scenario="bad_deploy_checkout",
        incident_id="incident-1",
        application_status="resolved",
        dataset_version="sentinel-sre-v1",
        scenario_version="1.0.0",
        dataset_split="train",
        dataset_sha256="a" * 64,
    )
    target = tmp_path / "oracle-results.jsonl"

    oracle.append_oracle_result(target, result)
    payload = json.loads(target.read_text().strip())

    assert payload["schema_version"] == 1
    assert payload["status"] == "UNRESOLVED"
    assert payload["application_status"] == "resolved"
    assert payload["false_resolved"] is True
    assert payload["dataset_version"] == "sentinel-sre-v1"
    assert payload["scenario_version"] == "1.0.0"
    assert payload["dataset_split"] == "train"
    assert payload["dataset_sha256"] == "a" * 64
    assert payload["baseline_healthy"] is True
    assert payload["probe"]["definition_sha256"]
    assert payload["observations"][0]["phase"] == "baseline"
    assert payload["observations"][1]["value"] == pytest.approx(0.40)


def test_live_benchmark_never_uses_incident_resolution_as_oracle():
    source = (
        Path(__file__).resolve().parents[1] / "benchmarks" / "sre_bench.py"
    ).read_text()

    assert "_wait_resolved" not in source
    assert "resolved_at" not in source
    assert "RecoveryOracleTracker" in source
    assert "result.status" in source
    assert "result.mttr_seconds" in source
