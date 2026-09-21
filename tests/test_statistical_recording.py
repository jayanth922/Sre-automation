#!/usr/bin/env python3
"""STATISTICAL_RECORDING has to write the rows the whole campaign rests on.

The only coverage this path had was a source grep -- `test_statistical_eval`
asserts `"_record_statistical_trial(" in source`, and `test_scenario_dataset`
asserts the flag flips to True. Both pass whether or not the writers write
anything. These tests drive both of them and read the rows back.

It matters beyond tidiness. The cheapest way out of the current deadlock --
severity rounds every incident up because diagnosis confidence is uncalibrated,
the round-up demands human approval, so no trial resolves -- is to build the
diagnosis calibration corpus, which needs roughly forty live observations.
Every one of them comes from `_record_confidence_observations`, and every trial
today stops at that approval gate. If a gated trial contributed nothing the
corpus could never fill and the deadlock would be permanent, so the test that
an unresolved trial still emits its diagnosis observation is the load-bearing
one here.

The fingerprint tests cover the opposite failure: both writers parse
`BENCH_CONFIG_FINGERPRINT` as a lowercase SHA-256, but the runner only checked
it was non-empty, so a typo aborted the run at the first record -- after an
incident had been provisioned, investigated and paid for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
# Appended, not prepended: `scoring` imports its siblings bare, so the
# directory has to be importable, but the repo root should still win any
# name collision.
sys.path.append(str(BENCHMARKS))

from scoring import RunScore  # noqa: E402

FINGERPRINT = "f" * 64
EXPERIMENT = {
    "BENCH_EXPERIMENT_ID": "exp-recording",
    "BENCH_CANDIDATE_ID": "full",
    "BENCH_CONFIG_FINGERPRINT": FINGERPRINT,
    "BENCH_PAIR_SEED": "seed-1",
}

_BENCH_ENV = (
    "BENCH_SCENARIOS",
    "BENCH_EXPERIMENT_ID",
    "BENCH_CANDIDATE_ID",
    "BENCH_CONFIG_FINGERPRINT",
    "BENCH_PAIR_SEED",
    "BENCH_TRIAL_RESULTS_PATH",
    "BENCH_CONFIDENCE_RESULTS_PATH",
)


def _load_runner(monkeypatch, tmp_path: Path, **env):
    """Import `sre_bench` fresh: it reads its recording config at import."""
    for key in _BENCH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BENCH_TRIAL_RESULTS_PATH", str(tmp_path / "trials.jsonl"))
    monkeypatch.setenv(
        "BENCH_CONFIDENCE_RESULTS_PATH", str(tmp_path / "confidence.jsonl")
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location(
        "sre_bench_recording_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _recording(monkeypatch, tmp_path: Path):
    return _load_runner(monkeypatch, tmp_path, **EXPERIMENT)


def _gated_score(scenario: str) -> RunScore:
    """What every trial produces today: stopped at the human approval gate.

    Unresolved, so no MTTR and -- by the trial schema's own coupling -- no
    overall structured grader verdict. Its diagnosis criterion was still
    graded against the oracle, and it still reported diagnosis confidence.
    """
    return RunScore(
        scenario=scenario,
        resolved=False,
        oracle_status="UNRESOLVED",
        application_status="investigating",
        rubric_version="v2",
        diagnosis_confidence=0.62,
        diagnosis_confidence_outcome=True,
        structured_grade={
            "criteria": {"diagnosis": {"state": "PASS"}},
        },
    )


def _resolved_score(scenario: str) -> RunScore:
    return RunScore(
        scenario=scenario,
        resolved=True,
        oracle_status="VERIFIED_RECOVERED",
        application_status="resolved",
        mttr_seconds=240.0,
        grader_status="PASS",
        rubric_version="v2",
        diagnosis_confidence=0.71,
        diagnosis_confidence_outcome=True,
        remediation_confidence=0.55,
        remediation_confidence_outcome=False,
        structured_grade={
            "criteria": {"diagnosis": {"state": "PASS"}},
        },
    )


_COMPLETE_TRACE = {
    "complete": True,
    "spans": 7,
    "cost_usd": 1.25,
    "records_sha256": "d" * 64,
    "artifact_path": "reports/trace-evidence.jsonl",
}

# The same run, except the evidence never closed. The cost is still quoted on
# purpose: the recorder has to drop it rather than pass it through.
_INCOMPLETE_TRACE = dict(_COMPLETE_TRACE, complete=False, spans=4, cost_usd=9.99)


def _rows(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_complete_trace_records_the_cost(monkeypatch, tmp_path):
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]

    module._record_statistical_trial(
        spec,
        _resolved_score(spec.name),
        trial_index=1,
        latency_seconds=412.5,
        trace_completeness=dict(_COMPLETE_TRACE),
    )

    rows = _rows(tmp_path / "trials.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["cost_usd"] == 1.25
    assert row["trace_complete"] is True
    assert row["trace_span_count"] == 7
    assert row["trace_evidence_sha256"] == "d" * 64
    assert row["latency_seconds"] == 412.5
    assert row["config_fingerprint"] == FINGERPRINT
    assert row["experiment_id"] == "exp-recording"
    assert row["dataset_sha256"] == module.DATASET.sha256
    assert row["diagnosis_status"] == "PASS"
    assert "trace_incomplete" not in row["failure_categories"]


def test_an_incomplete_trace_records_no_cost_and_says_why(monkeypatch, tmp_path):
    """Fail closed: an unverifiable trace must not contribute a cost figure."""
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]

    module._record_statistical_trial(
        spec,
        _resolved_score(spec.name),
        trial_index=1,
        latency_seconds=412.5,
        trace_completeness=dict(_INCOMPLETE_TRACE),
    )

    row = _rows(tmp_path / "trials.jsonl")[0]
    assert row["cost_usd"] is None, "a quoted cost on an open trace must be dropped"
    assert row["trace_complete"] is False
    assert "trace_incomplete" in row["failure_categories"]


def test_a_missing_trace_payload_is_treated_as_incomplete(monkeypatch, tmp_path):
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]

    module._record_statistical_trial(
        spec,
        _gated_score(spec.name),
        trial_index=1,
        latency_seconds=300.0,
        trace_completeness=None,
    )

    row = _rows(tmp_path / "trials.jsonl")[0]
    assert row["cost_usd"] is None
    assert row["trace_complete"] is False
    assert row["trace_span_count"] == 0
    assert row["diagnosis_status"] == "PASS"
    assert set(row["failure_categories"]) == {"unresolved", "trace_incomplete"}


def test_missing_or_malformed_structured_diagnosis_fails_closed(monkeypatch, tmp_path):
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]
    score = _gated_score(spec.name)
    score.structured_grade = {"criteria": {"diagnosis": {"state": "MAYBE"}}}

    module._record_statistical_trial(
        spec,
        score,
        trial_index=1,
        latency_seconds=300.0,
        trace_completeness=dict(_COMPLETE_TRACE),
    )

    assert _rows(tmp_path / "trials.jsonl")[0]["diagnosis_status"] == (
        "INSUFFICIENT_EVIDENCE"
    )


def test_a_gated_trial_still_contributes_a_diagnosis_observation(monkeypatch, tmp_path):
    """The corpus that unlocks autonomy has to be reachable from where we are.

    Nothing resolves today. If an unresolved trial recorded no confidence
    observation there would be no way to accumulate the forty the diagnosis
    artifact needs, and the escalate-then-block loop would never open.
    """
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]
    score = _gated_score(spec.name)

    module._record_statistical_trial(
        spec,
        score,
        trial_index=2,
        latency_seconds=300.0,
        trace_completeness=dict(_COMPLETE_TRACE),
    )
    module._record_confidence_observations(spec, score, trial_index=2)

    records = _rows(tmp_path / "confidence.jsonl")
    assert len(records) == 1, "the never-reached remediation must not be recorded"
    record = records[0]
    assert record["task"] == "diagnosis"
    assert record["raw_confidence"] == 0.62
    assert record["outcome"] is True
    assert record["evidence_source"] == "live_benchmark"
    assert record["config_fingerprint"] == FINGERPRINT
    assert record["dataset_sha256"] == module.DATASET.sha256
    # Same trial, so the observation and the trial row have to join.
    assert record["pair_id"] == _rows(tmp_path / "trials.jsonl")[0]["pair_id"]


def test_an_executed_remediation_is_recorded_too(monkeypatch, tmp_path):
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]

    module._record_confidence_observations(
        spec, _resolved_score(spec.name), trial_index=1
    )

    records = _rows(tmp_path / "confidence.jsonl")
    assert {record["task"] for record in records} == {"diagnosis", "remediation"}
    remediation = next(r for r in records if r["task"] == "remediation")
    assert remediation["raw_confidence"] == 0.55
    assert remediation["outcome"] is False


def test_a_confidence_without_a_graded_outcome_is_not_recorded(monkeypatch, tmp_path):
    """An unpaired number would enter the corpus as evidence of nothing."""
    module = _recording(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]
    score = _gated_score(spec.name)
    score.diagnosis_confidence_outcome = None

    module._record_confidence_observations(spec, score, trial_index=1)

    assert _rows(tmp_path / "confidence.jsonl") == []


def test_nothing_is_written_without_the_experiment_env(monkeypatch, tmp_path):
    module = _load_runner(monkeypatch, tmp_path)
    spec = module.SCENARIOS[0]
    assert module.STATISTICAL_RECORDING is False

    module._record_statistical_trial(
        spec,
        _resolved_score(spec.name),
        trial_index=1,
        latency_seconds=1.0,
        trace_completeness=dict(_COMPLETE_TRACE),
    )
    module._record_confidence_observations(
        spec, _resolved_score(spec.name), trial_index=1
    )

    assert not (tmp_path / "trials.jsonl").exists()
    assert not (tmp_path / "confidence.jsonl").exists()


def test_a_malformed_config_fingerprint_is_refused_before_the_run(
    monkeypatch, tmp_path
):
    """Otherwise the first write refuses it, one paid incident too late."""
    env = dict(EXPERIMENT, BENCH_CONFIG_FINGERPRINT="fingerprint-1")

    with pytest.raises(RuntimeError, match="SHA-256"):
        _load_runner(monkeypatch, tmp_path, **env)


def test_an_uppercase_digest_is_refused(monkeypatch, tmp_path):
    """`_sha256` is case-sensitive, so this would fail at the first write too."""
    env = dict(EXPERIMENT, BENCH_CONFIG_FINGERPRINT="F" * 64)

    with pytest.raises(RuntimeError, match="SHA-256"):
        _load_runner(monkeypatch, tmp_path, **env)
