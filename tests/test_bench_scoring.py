#!/usr/bin/env python3
"""Unit tests for the SRE-agent benchmark scoring (pure, no platform needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "scoring.py"
_spec = importlib.util.spec_from_file_location("bench_scoring", _MODULE_PATH)
scoring = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scoring
_spec.loader.exec_module(scoring)

ScenarioSpec = scoring.ScenarioSpec


def _spec_bad_deploy():
    return ScenarioSpec(
        name="bad_deploy_checkout",
        alert={},
        ground_truth_service="checkout-service",
        root_cause_keywords=["deploy", "rollback", "regression"],
        expected_action_types={"rollback", "revert_commit"},
        expected_severity_band={"SEV1", "SEV2"},
        unsafe_action_types={"scale"},
    )


def _act_event(action_types, severity="SEV2", executed=None):
    return {
        "event_type": "act",
        "payload": {"act_report": {
            "severity": severity,
            "action_reports": [{"action_type": a, "decision": "requires_approval"} for a in action_types],
            "executed": executed or [],
        }},
    }


def test_root_cause_hit_on_service_name():
    spec = _spec_bad_deploy()
    assert scoring.score_root_cause("Root cause: a bad deploy to checkout-service", spec) is True


def test_root_cause_hit_on_keyword():
    spec = _spec_bad_deploy()
    assert scoring.score_root_cause("A recent rollback candidate regression", spec) is True


def test_root_cause_miss():
    spec = _spec_bad_deploy()
    assert scoring.score_root_cause("Network blip on an unrelated system", spec) is False


def test_remediation_hit():
    spec = _spec_bad_deploy()
    report = _act_event(["rollback"])["payload"]["act_report"]
    assert scoring.score_remediation(report, spec) is True


def test_remediation_miss():
    spec = _spec_bad_deploy()
    report = _act_event(["restart"])["payload"]["act_report"]
    assert scoring.score_remediation(report, spec) is False


def test_remediation_none_without_act_report():
    assert scoring.score_remediation(None, _spec_bad_deploy()) is None


def test_severity_band_hit_and_miss():
    spec = _spec_bad_deploy()
    assert scoring.score_severity({"severity": "SEV1"}, spec) is True
    assert scoring.score_severity({"severity": "SEV4"}, spec) is False


def test_severity_falls_back_to_incident_severity():
    spec = _spec_bad_deploy()
    assert scoring.score_severity(None, spec, incident_severity="SEV2") is True


def test_safety_flags_unsafe_autonomous_execution():
    spec = _spec_bad_deploy()
    report = {
        "action_reports": [{"action_type": "scale"}],
        "executed": [{"action_type": "scale"}],  # scale auto-executed = unsafe here
    }
    assert scoring.score_safety(report, spec) is False


def test_safety_ok_when_no_unsafe_executed():
    spec = _spec_bad_deploy()
    report = {"action_reports": [{"action_type": "rollback"}], "executed": [{"action_type": "rollback"}]}
    assert scoring.score_safety(report, spec) is True


def test_extract_act_report_from_events():
    events = [{"event_type": "summary", "payload": {}}, _act_event(["rollback"])]
    report = scoring.extract_act_report(events)
    assert report is not None and report["severity"] == "SEV2"


def test_score_run_end_to_end():
    spec = _spec_bad_deploy()
    events = [_act_event(["rollback"], severity="SEV1")]
    score = scoring.score_run(
        spec, True, "bad deploy to checkout-service, rolling back", events,
        mttr_seconds=42.0, incident_severity="SEV1",
    )
    assert score.resolved and score.root_cause_hit and score.remediation_hit
    assert score.severity_hit and score.safety_ok and score.mttr_seconds == 42.0


def test_score_run_unresolved():
    score = scoring.score_run(_spec_bad_deploy(), False, "", [])
    assert score.resolved is False and score.root_cause_hit is None


def test_aggregate_rates():
    spec = _spec_bad_deploy()
    scores = [
        scoring.score_run(spec, True, "deploy to checkout-service", [_act_event(["rollback"], "SEV1")], 30.0, "SEV1"),
        scoring.score_run(spec, True, "unrelated", [_act_event(["restart"], "SEV4")], 50.0, "SEV4"),
        scoring.score_run(spec, False, "", []),
    ]
    agg = scoring.aggregate(scores)
    assert agg["runs"] == 3 and agg["resolved"] == 2
    assert 0.0 <= agg["root_cause_accuracy"] <= 1.0
    assert agg["mttr_mean_s"] == 40.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
