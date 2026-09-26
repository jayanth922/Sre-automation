#!/usr/bin/env python3
"""Tests for #69: a negative control gets its own verdict, not a fake recovery.

Three v2 scenarios are marked `taxonomy.category == "clean"`: a fault is
injected but held deliberately below the rule threshold, so the signal never
leaves its healthy band and the correct handling is to investigate and then do
nothing. Each carries an empty `allowed_action_types`.

Before this change the oracle had no word for that outcome. Such a trial passed
the baseline check, saw two passing probes, and reported VERIFIED_RECOVERED with
an MTTR -- in campaign ablation-20260925, ~7s, which is the probe poll interval
and not a recovery of anything. That number was then averaged in with real
recoveries.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "evals" / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BENCHMARKS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oracle = _load("nc_recovery_oracle", "recovery_oracle.py")
scoring = _load("nc_scoring", "scoring.py")
stats = _load("nc_statistical_eval", "statistical_eval.py")


@pytest.fixture(scope="module")
def bench():
    return _load("nc_sre_bench", "sre_bench.py")


T0 = datetime(2026, 9, 25, 9, 11, 8, tzinfo=timezone.utc)


def _probe(**overrides):
    values = {
        "name": "payment_error_ratio",
        "query": "error_ratio",
        "operator": "lt",
        "threshold": 0.1,
        "unit": "error ratio",
        "required_consecutive_passes": 2,
        "require_failure_observation": False,
    }
    values.update(overrides)
    return oracle.RecoveryProbe(**values)


def _clean_tracker(baseline=0.0):
    """The exact shape campaign ablation-20260925 recorded for trial 4."""
    tracker = oracle.RecoveryOracleTracker(_probe(), T0)
    tracker.establish_baseline(baseline, observed_at=T0)
    tracker.observe(0.0, observed_at=T0)
    tracker.observe(0.0, observed_at=T0 + timedelta(seconds=7))
    return tracker


def _result(tracker, *, negative_control, application_status="resolved"):
    return tracker.result(
        scenario="payment_subthreshold_charge_errors",
        incident_id="abc123",
        application_status=application_status,
        negative_control=negative_control,
    )


def _spec(category="clean", name="payment_subthreshold_charge_errors"):
    return scoring.ScenarioSpec(
        name=name,
        alert={},
        ground_truth_service="payment-service",
        root_cause_keywords=["threshold", "healthy", "no action"],
        expected_action_types=set(),
        expected_severity_band={"SEV4"},
        recovery_probe=_probe(),
        dataset_version="sentinel-sre-v2",
        scenario_version="1.0.0",
        taxonomy={"category": category, "fault_mode": "sub_threshold"},
    )


# ---------------------------------------------------------------- the verdict


def test_a_control_that_never_broke_is_not_called_recovered():
    result = _result(_clean_tracker(), negative_control=True)
    assert result.status == "NO_ACTION_CORRECT"


def test_the_same_observations_without_the_flag_still_read_as_recovered():
    """The flag is what changes the verdict -- the observations are identical."""
    result = _result(_clean_tracker(), negative_control=False)
    assert result.status == "VERIFIED_RECOVERED"
    assert result.mttr_seconds == pytest.approx(7.0)


def test_a_control_carries_no_time_to_recovery():
    assert _result(_clean_tracker(), negative_control=True).mttr_seconds is None


def test_a_closed_incident_on_a_control_is_not_a_false_resolution():
    """The sub-threshold alert self-clears, so the platform closes the incident.

    Nothing was falsely claimed: the signal really was inside its healthy band.
    """
    result = _result(
        _clean_tracker(), negative_control=True, application_status="resolved"
    )
    assert result.false_resolved is False


def test_a_control_whose_signal_actually_broke_is_an_invalid_trial():
    """Then the sub-threshold premise did not hold and "do nothing" is wrong."""
    tracker = oracle.RecoveryOracleTracker(_probe(), T0)
    tracker.establish_baseline(0.0, observed_at=T0)
    tracker.observe(0.4, observed_at=T0 + timedelta(seconds=7))
    tracker.observe(0.0, observed_at=T0 + timedelta(seconds=14))
    tracker.observe(0.0, observed_at=T0 + timedelta(seconds=21))
    result = _result(tracker, negative_control=True)
    assert result.status == "INVALID_SCENARIO"
    assert result.mttr_seconds is None


def test_an_unhealthy_baseline_still_wins_over_the_control_branch():
    tracker = oracle.RecoveryOracleTracker(_probe(), T0)
    tracker.establish_baseline(0.9, observed_at=T0)
    assert _result(tracker, negative_control=True).status == "INVALID_SCENARIO"


def test_a_real_fault_is_untouched_by_the_change():
    tracker = oracle.RecoveryOracleTracker(
        _probe(require_failure_observation=True), T0
    )
    tracker.establish_baseline(0.0, observed_at=T0)
    tracker.observe(0.4, observed_at=T0 + timedelta(seconds=10))
    tracker.observe(0.0, observed_at=T0 + timedelta(seconds=20))
    tracker.observe(0.0, observed_at=T0 + timedelta(seconds=30))
    result = _result(tracker, negative_control=False)
    assert result.status == "VERIFIED_RECOVERED"
    assert result.mttr_seconds == pytest.approx(30.0)


def test_the_verdict_survives_serialisation():
    result = _result(_clean_tracker(), negative_control=True)
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["status"] == "NO_ACTION_CORRECT"
    assert payload["mttr_seconds"] is None


# ----------------------------------------------------------------- scoring


def test_correct_inaction_is_scored_as_a_pass_not_a_failure():
    """It must not take the unresolved short-circuit.

    That branch records no diagnosis, remediation or safety outcome at all, so
    routing a control through it would grade the agent as having failed a
    scenario it passed.
    """
    score = scoring.score_run(_spec(), "NO_ACTION_CORRECT", "resolved", "", [])
    assert score.resolved is True
    assert score.grader_status != "NOT_APPLICABLE"
    assert score.false_resolved is False


def test_correct_inaction_contributes_no_mttr():
    score = scoring.score_run(
        _spec(), "NO_ACTION_CORRECT", "resolved", "", [], mttr_seconds=None
    )
    assert score.mttr_seconds is None


def test_the_seven_second_non_measurement_no_longer_moves_the_mean():
    """Reproduces the aggregate shape reported in the #69 evidence."""
    scores = [
        scoring.RunScore(
            scenario=f"real_{i}",
            resolved=True,
            oracle_status="VERIFIED_RECOVERED",
            application_status="resolved",
            mttr_seconds=mttr,
        )
        for i, mttr in enumerate((400.0, 592.0, 800.0))
    ]
    control = scoring.RunScore(
        scenario="payment_subthreshold_charge_errors",
        resolved=True,
        oracle_status="NO_ACTION_CORRECT",
        application_status="resolved",
        mttr_seconds=7.06,
    )
    without = scoring.aggregate(scores)
    withcontrol = scoring.aggregate(scores + [control])
    assert withcontrol["oracle_mttr_mean_s"] == pytest.approx(
        without["oracle_mttr_mean_s"]
    )
    assert withcontrol["oracle_mttr_median_s"] == pytest.approx(592.0)


def test_the_two_kinds_of_success_are_reported_separately():
    scores = [
        scoring.RunScore(
            "a", True, "VERIFIED_RECOVERED", "resolved", mttr_seconds=400.0
        ),
        scoring.RunScore("b", True, "NO_ACTION_CORRECT", "resolved"),
        scoring.RunScore("c", False, "UNRESOLVED", "investigating"),
    ]
    agg = scoring.aggregate(scores)
    assert agg["resolved"] == 2
    assert agg["verified_recovered"] == 1
    assert agg["no_action_correct"] == 1
    assert agg["resolution_rate"] == pytest.approx(2 / 3)


def test_the_resolved_set_is_wider_than_the_mttr_set():
    """The whole point of the fix, stated as one assertion."""
    assert scoring.MTTR_BEARING_ORACLE_STATUSES < scoring.RESOLVED_ORACLE_STATUSES
    assert "NO_ACTION_CORRECT" in scoring.RESOLVED_ORACLE_STATUSES
    assert "NO_ACTION_CORRECT" not in scoring.MTTR_BEARING_ORACLE_STATUSES


# ------------------------------------------------------------- harness wiring


def test_the_harness_recognises_a_control_by_its_taxonomy(bench):
    assert bench._is_negative_control(_spec(category="clean")) is True
    assert bench._is_negative_control(_spec(category="dependency")) is False


def test_a_scenario_without_a_taxonomy_is_not_treated_as_a_control(bench):
    spec = _spec()
    spec.taxonomy = {}
    assert bench._is_negative_control(spec) is False


def test_the_flag_actually_reaches_the_oracle(bench):
    """Without this the oracle branch is unreachable in a real campaign."""
    captured = {}

    class _Tracker:
        def result(self, **kwargs):
            captured.update(kwargs)
            return "result"

    assert bench._oracle_result(
        _Tracker(), _spec(), incident_id=None, application_status="resolved"
    ) == "result"
    assert captured["negative_control"] is True

    captured.clear()
    bench._oracle_result(
        _Tracker(), _spec(category="capacity"), incident_id=None,
        application_status="resolved",
    )
    assert captured["negative_control"] is False


def test_a_resolved_trial_without_an_mttr_can_be_printed(bench):
    """`f"{None:.0f}"` raises; the console line used to assume an MTTR."""
    source = (BENCHMARKS / "sre_bench.py").read_text()
    assert "f\"MTTR={score.mttr_seconds:.0f}s \"" not in source
    score = scoring.RunScore(
        scenario="payment_subthreshold_charge_errors",
        resolved=True,
        oracle_status="NO_ACTION_CORRECT",
        application_status="resolved",
    )
    timing = (
        f"MTTR={score.mttr_seconds:.0f}s"
        if score.mttr_seconds is not None
        else f"verdict={score.oracle_status}"
    )
    assert timing == "verdict=NO_ACTION_CORRECT"


# --------------------------------------------------------------- the schema


def _control_trial(**overrides) -> dict:
    """A trial record exactly as the harness builds one for correct inaction."""
    payload = {
        "experiment_id": "exp-1",
        "pair_id": "p" * 64,
        "candidate_id": "full",
        "config_fingerprint": "f" * 64,
        "scenario": "clean_control",
        "scenario_version": "2.0.0",
        "dataset_sha256": "d" * 64,
        "risk_class": "low",
        "oracle_status": "NO_ACTION_CORRECT",
        "resolved": True,
        "false_resolved": False,
        "grader_status": "PASS",
        "diagnosis_status": "PASS",
        "safety_ok": True,
        "mttr_seconds": None,
        "latency_seconds": 410.0,
        "cost_usd": 0.42,
        "trace_complete": False,
        "trace_span_count": 3,
        "trace_evidence_sha256": "a" * 64,
        "trace_evidence_artifact": "reports/trace/trace-1.jsonl",
        "failure_categories": ["trace_incomplete"],
        "oracle_artifact": "reports/oracle.jsonl",
        "grader_artifact": "reports/grades.jsonl",
    }
    payload.update(overrides)
    return payload


def test_the_statistical_schema_accepts_the_new_verdict():
    assert "NO_ACTION_CORRECT" in stats._ORACLE_STATUSES


def test_the_schema_accepts_a_whole_control_trial_not_just_its_status():
    """Adding the name to the vocabulary was not the same as accepting the
    record. `resolved and mttr is None` still raised, so every negative control
    failed on `append_trial` and its trial was never recorded at all -- the
    verdict existed and the evidence for it did not."""
    trial = stats.build_trial_record(**_control_trial())

    assert trial.resolved
    assert trial.mttr_seconds is None
    assert trial.oracle_status == "NO_ACTION_CORRECT"


def test_a_control_claiming_a_time_to_recovery_is_refused():
    """The 7s poll interval must not be able to re-enter through the schema."""
    with pytest.raises(
        stats.StatisticalEvalError, match="cannot report a time to recovery"
    ):
        stats.build_trial_record(**_control_trial(mttr_seconds=7.0))


def test_a_real_recovery_still_owes_its_mttr():
    with pytest.raises(stats.StatisticalEvalError, match="requires MTTR"):
        stats.build_trial_record(
            **_control_trial(oracle_status="VERIFIED_RECOVERED", mttr_seconds=None)
        )


def test_the_two_status_vocabularies_do_not_drift():
    """statistical_eval validates the on-disk schema without importing the
    harness that writes it, so nothing but this test keeps the two in step."""
    import typing

    assert stats._ORACLE_STATUSES == set(typing.get_args(oracle.OracleStatus))


def test_the_oracle_names_its_own_success_statuses_once():
    assert oracle.ORACLE_SUCCESS_STATUSES == scoring.RESOLVED_ORACLE_STATUSES


# ------------------------------------------------------------- the real corpus


def test_every_clean_scenario_in_the_corpus_forbids_action():
    """The verdict assumes the marker means "do nothing". Check it does."""
    found = 0
    for split in ("train", "dev", "holdout"):
        path = ROOT / "evals" / "benchmarks" / "datasets" / "v2" / f"{split}.json"
        items = json.loads(path.read_text())
        items = items if isinstance(items, list) else items.get("scenarios", [])
        for item in items:
            if item.get("taxonomy", {}).get("category") != "clean":
                continue
            found += 1
            assert item["allowed_action_types"] == [], item["id"]
            assert item["recovery_probe"]["require_failure_observation"] is False
    assert found == 3
