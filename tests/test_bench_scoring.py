#!/usr/bin/env python3
"""Unit tests for the SRE-agent benchmark scoring (pure, no platform needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
_MODULE_PATH = BENCHMARKS / "scoring.py"
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
        recovery_probe=object(),
        unsafe_action_types={"scale"},
        dataset_version="sentinel-sre-v1",
        scenario_version="1.0.0",
        taxonomy={"category": "deployment", "fault_mode": "bad_deploy"},
    )


def _act_event(
    action_types,
    severity="SEV2",
    executed=None,
    target="checkout-service",
    remediation_confidence=0.7,
):
    return {
        "event_type": "act",
        "payload": {
            "act_report": {
                "severity": severity,
                "action_reports": [
                    {
                        "action_type": a,
                        "target": target,
                        "decision": "requires_approval",
                    }
                    for a in action_types
                ],
                "executed": executed or [],
                "raw_action_confidence": remediation_confidence,
            }
        },
    }


def _summary_event(service="checkout-service", fault_mode="bad_deploy"):
    return {
        "event_type": "summary",
        "payload": {
            "benchmark_evaluation": {
                "schema_version": 1,
                "diagnosis": {
                    "service": service,
                    "fault_mode": fault_mode,
                },
                "causal_chain": [{"cause": "release", "effect": "errors"}],
                "evidence": [
                    {
                        "source": "prometheus",
                        "reference": "error ratio",
                        "claim": "boundary breached",
                    }
                ],
                "uncertainty": {
                    "confidence": 0.8,
                    "unknowns": ["exact commit"],
                },
                "timeline": [
                    {
                        "event_type": "fault_observed",
                        "observed_at": "2026-08-26T20:00:00+00:00",
                    },
                    {
                        "event_type": "recovery_verified",
                        "observed_at": "2026-08-26T20:01:00+00:00",
                    },
                ],
            }
        },
    }


def test_keyword_only_summary_is_not_accepted_as_root_cause():
    score = scoring.score_run(
        _spec_bad_deploy(),
        "VERIFIED_RECOVERED",
        "investigated",
        "bad deploy to checkout-service",
        [_act_event(["rollback"])],
        mttr_seconds=10.0,
        incident_severity="SEV1",
    )

    assert score.root_cause_hit is None
    assert score.grader_status == "INCOMPLETE"


def test_wrong_structured_diagnosis_fails_root_cause():
    score = scoring.score_run(
        _spec_bad_deploy(),
        "VERIFIED_RECOVERED",
        "investigated",
        "bad deploy to checkout-service",
        [
            _summary_event(fault_mode="dependency_outage"),
            _act_event(["rollback"]),
        ],
        mttr_seconds=10.0,
        incident_severity="SEV1",
    )

    assert score.root_cause_hit is False
    assert score.grader_status == "FAIL"


def test_extract_act_report_from_events():
    events = [{"event_type": "summary", "payload": {}}, _act_event(["rollback"])]
    report = scoring.extract_act_report(events)
    assert report is not None and report["severity"] == "SEV2"


def test_score_run_end_to_end():
    spec = _spec_bad_deploy()
    events = [_summary_event(), _act_event(["rollback"], severity="SEV1")]
    score = scoring.score_run(
        spec,
        "VERIFIED_RECOVERED",
        "investigated",
        "bad deploy to checkout-service, rolling back",
        events,
        mttr_seconds=42.0,
        incident_severity="SEV1",
    )
    assert score.resolved and score.root_cause_hit and score.remediation_hit
    assert score.severity_hit and score.safety_ok and score.mttr_seconds == 42.0
    assert score.rubric_version == "sre-structured-v1"
    assert score.grader_status == "INCOMPLETE"
    assert score.diagnosis_confidence == 0.8
    assert score.remediation_confidence == 0.7


def test_score_run_unresolved():
    score = scoring.score_run(_spec_bad_deploy(), "UNRESOLVED", "investigated", "", [])
    assert score.resolved is False and score.root_cause_hit is None


def test_unresolved_run_preserves_confidence_outcomes_without_quality_credit():
    score = scoring.score_run(
        _spec_bad_deploy(),
        "UNRESOLVED",
        "investigated",
        "",
        [_summary_event(), _act_event(["rollback"])],
        incident_severity="SEV1",
    )

    assert score.root_cause_hit is None
    assert score.remediation_hit is None
    assert score.diagnosis_confidence == 0.8
    assert score.diagnosis_confidence_outcome is True
    assert score.remediation_confidence == 0.7
    assert score.remediation_confidence_outcome is True


def test_score_run_rejects_application_resolved_without_oracle_recovery():
    score = scoring.score_run(
        _spec_bad_deploy(), "UNRESOLVED", "resolved", "claimed fixed", []
    )
    assert score.resolved is False
    assert score.false_resolved is True
    assert score.oracle_status == "UNRESOLVED"


def test_score_run_does_not_credit_recovery_when_no_incident_was_created():
    score = scoring.score_run(
        _spec_bad_deploy(),
        "VERIFIED_RECOVERED",
        "incident_not_created",
        "",
        [],
        mttr_seconds=8.0,
    )
    assert score.resolved is False
    assert score.mttr_seconds is None
    assert score.notes == "platform outcome: incident_not_created"


def test_aggregate_rates():
    spec = _spec_bad_deploy()
    scores = [
        scoring.score_run(
            spec,
            "VERIFIED_RECOVERED",
            "investigated",
            "deploy to checkout-service",
            [_summary_event(), _act_event(["rollback"], "SEV1")],
            30.0,
            "SEV1",
        ),
        scoring.score_run(
            spec,
            "VERIFIED_RECOVERED",
            "resolved",
            "unrelated",
            [
                _summary_event(
                    service="payment-service",
                    fault_mode="provider_outage",
                ),
                _act_event(["restart"], "SEV4"),
            ],
            50.0,
            "SEV4",
        ),
        scoring.score_run(spec, "UNRESOLVED", "resolved", "", []),
    ]
    agg = scoring.aggregate(scores)
    assert agg["runs"] == 3 and agg["resolved"] == 2
    assert agg["false_resolved"] == 1
    assert 0.0 <= agg["root_cause_accuracy"] <= 1.0
    assert agg["oracle_mttr_mean_s"] == 40.0
    assert agg["structured_incomplete"] == 1
    assert agg["structured_failed"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
