#!/usr/bin/env python3
"""Tests for paired A05 statistical evaluation and promotion gates."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
_MODULE_PATH = BENCHMARKS / "statistical_eval.py"
_spec = importlib.util.spec_from_file_location("statistical_eval", _MODULE_PATH)
evaluation = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = evaluation
_spec.loader.exec_module(evaluation)


def _trial(
    pair,
    candidate,
    *,
    fingerprint,
    resolved=True,
    grader_status=None,
    safety_ok=True,
    risk_class="high",
    latency=10.0,
    mttr=20.0,
    cost=None,
    failure_categories=(),
):
    return evaluation.TrialRecord(
        experiment_id="exp-1",
        pair_id=f"pair-{pair:03d}",
        candidate_id=candidate,
        config_fingerprint=fingerprint,
        scenario="bad_deploy_checkout",
        scenario_version="1.0.0",
        dataset_sha256="d" * 64,
        risk_class=risk_class,
        oracle_status="VERIFIED_RECOVERED" if resolved else "UNRESOLVED",
        resolved=resolved,
        false_resolved=False,
        grader_status=grader_status or ("PASS" if resolved else "NOT_APPLICABLE"),
        safety_ok=safety_ok,
        mttr_seconds=mttr if resolved else None,
        latency_seconds=latency,
        cost_usd=cost,
        failure_categories=tuple(failure_categories),
        oracle_artifact="reports/oracle.jsonl",
        grader_artifact="reports/grades.jsonl",
    )


def _paired_trials(count=24):
    trials = []
    for pair in range(count):
        trials.extend(
            [
                _trial(
                    pair,
                    "baseline",
                    fingerprint="a" * 64,
                    latency=12.0,
                    mttr=24.0,
                    cost=0.20,
                ),
                _trial(
                    pair,
                    "candidate",
                    fingerprint="b" * 64,
                    latency=10.0,
                    mttr=20.0,
                    cost=0.15,
                ),
            ]
        )
    return tuple(trials)


def _artifact(records):
    return evaluation.ArtifactEvidence(
        path="reports/trials.jsonl",
        sha256="f" * 64,
        records=records,
    )


def test_wilson_and_pass_k_are_bounded():
    lower, upper = evaluation.wilson_interval(8, 10)

    assert 0 <= lower < 0.8 < upper <= 1
    assert evaluation.pass_at_k(2, 5, 3) == pytest.approx(0.9)
    assert evaluation.pass_power_k(2, 5, 3) == 0.0


def test_configuration_fingerprint_excludes_trial_input_and_trace():
    manifest = {
        "provenance": {"code_sha": "abc"},
        "models": {"routes": ["model-a"]},
        "tools": {"schemas": ["tool-a"]},
        "runtime": {"policy": "prod"},
        "input": {"sha256": "first"},
        "trace": {"root_trace_id": "trace-a"},
    }
    changed_trial = {
        **manifest,
        "input": {"sha256": "second"},
        "trace": {"root_trace_id": "trace-b"},
    }

    assert evaluation.configuration_fingerprint(
        manifest
    ) == evaluation.configuration_fingerprint(changed_trial)


def test_bootstrap_interval_is_reproducible():
    first = evaluation.bootstrap_mean_interval(
        [-1.0, 0.0, 1.0, 2.0], seed=41, samples=500
    )
    second = evaluation.bootstrap_mean_interval(
        [-1.0, 0.0, 1.0, 2.0], seed=41, samples=500
    )

    assert first == second


def test_pair_ids_are_stable_across_candidate_runs():
    values = {
        "experiment_id": "exp-1",
        "dataset_sha256": "d" * 64,
        "scenario": "bad_deploy_checkout",
        "scenario_version": "1.0.0",
        "trial_index": 1,
        "pair_seed": "seed-41",
    }

    assert evaluation.make_pair_id(**values) == evaluation.make_pair_id(**values)
    assert evaluation.make_pair_id(**values) != evaluation.make_pair_id(
        **{**values, "trial_index": 2}
    )


def test_randomized_schedule_is_reproducible_and_complete():
    first = evaluation.build_trial_schedule(
        ["scenario-a", "scenario-b"],
        runs_per_scenario=3,
        pair_seed="seed-41",
        dataset_sha256="d" * 64,
        randomize=True,
    )
    second = evaluation.build_trial_schedule(
        ["scenario-a", "scenario-b"],
        runs_per_scenario=3,
        pair_seed="seed-41",
        dataset_sha256="d" * 64,
        randomize=True,
    )

    assert first == second
    assert set(first) == {
        ("scenario-a", 1),
        ("scenario-a", 2),
        ("scenario-a", 3),
        ("scenario-b", 1),
        ("scenario-b", 2),
        ("scenario-b", 3),
    }


def test_complete_noninferior_candidate_can_promote():
    trials = _paired_trials(count=80)

    report = evaluation.compare_candidates(
        trials,
        baseline_id="baseline",
        candidate_id="candidate",
        artifact=_artifact(len(trials)),
        minimum_pairs=20,
        maximum_ci_width=0.2,
    )

    assert report["paired"]["pair_count"] == 80
    assert report["paired"]["recovery"]["mean_delta"] == 0.0
    assert report["paired"]["latency_seconds"]["mean_delta"] == -2.0
    assert report["paired"]["oracle_mttr_seconds"]["mean_delta"] == -4.0
    assert report["paired"]["cost_usd"]["mean_delta"] == pytest.approx(-0.05)
    assert report["candidate"]["config_fingerprint"] == "b" * 64
    assert report["candidate"]["scenarios"]["bad_deploy_checkout"]["runs"] == 80
    assert report["raw_artifacts"][0]["sha256"] == "f" * 64
    assert report["release_decision"]["status"] == "PROMOTE"


def test_incomplete_structured_grades_and_low_sample_block_promotion():
    trials = list(_paired_trials(count=3))
    trials[-1] = _trial(
        2,
        "candidate",
        fingerprint="b" * 64,
        grader_status="INCOMPLETE",
        failure_categories=("structured_incomplete",),
    )

    report = evaluation.compare_candidates(
        tuple(trials),
        baseline_id="baseline",
        candidate_id="candidate",
        artifact=_artifact(len(trials)),
        minimum_pairs=20,
    )

    reasons = " ".join(report["release_decision"]["reasons"])
    assert report["release_decision"]["status"] == "BLOCK"
    assert "paired trials" in reasons
    assert "structured grades" in reasons
    assert report["candidate"]["failure_categories"] == {"structured_incomplete": 1}


def test_any_candidate_safety_failure_blocks_promotion():
    trials = list(_paired_trials())
    trials[-1] = _trial(
        23,
        "candidate",
        fingerprint="b" * 64,
        safety_ok=False,
        failure_categories=("safety_failure",),
    )

    report = evaluation.compare_candidates(
        tuple(trials),
        baseline_id="baseline",
        candidate_id="candidate",
        artifact=_artifact(len(trials)),
    )

    assert "candidate has a safety failure" in report["release_decision"]["reasons"]


def test_critical_slice_regression_blocks_promotion():
    trials = list(_paired_trials())
    trials[-1] = _trial(
        23,
        "candidate",
        fingerprint="b" * 64,
        resolved=False,
        failure_categories=("unresolved",),
    )

    report = evaluation.compare_candidates(
        tuple(trials),
        baseline_id="baseline",
        candidate_id="candidate",
        artifact=_artifact(len(trials)),
        noninferiority_margin=1.0,
    )

    assert (
        "candidate regresses a high/critical-risk slice"
        in report["release_decision"]["reasons"]
    )


def test_missing_pair_is_rejected():
    trials = _paired_trials()[:-1]

    with pytest.raises(evaluation.StatisticalEvalError, match="incomplete"):
        evaluation.compare_candidates(
            trials,
            baseline_id="baseline",
            candidate_id="candidate",
            artifact=_artifact(len(trials)),
        )


def test_trial_jsonl_is_strict_and_content_addressed(tmp_path):
    path = tmp_path / "trials.jsonl"
    record = _trial(1, "baseline", fingerprint="a" * 64)
    evaluation.append_trial(path, record)

    loaded, artifact = evaluation.load_trials(path)

    assert loaded == (record,)
    assert artifact.records == 1
    assert len(artifact.sha256) == 64

    payload = json.loads(path.read_text())
    payload["unexpected"] = True
    path.write_text(json.dumps(payload) + "\n")
    with pytest.raises(evaluation.StatisticalEvalError, match="keys"):
        evaluation.load_trials(path)


def test_live_runner_records_explicit_experiment_provenance():
    source = (BENCHMARKS / "sre_bench.py").read_text()

    assert "BENCH_EXPERIMENT_ID" in source
    assert "BENCH_CONFIG_FINGERPRINT" in source
    assert "make_pair_id(" in source
    assert "_record_statistical_trial(" in source
