#!/usr/bin/env python3
"""The root-trace artifact the release gate requires had no producer.

`release_gate.py` demands five kinds of evidence, and `root_traces` is one of
them. The only thing in the repository that had ever written one was
`make_release_fixtures.py` -- so the gate had only ever run against fixtures it
generated itself. Neither paid campaign emitted the artifact, and neither
attestation mentions the gate, because the gate could not be run on either.

Two failures are covered here, because a producer alone would not have been
enough:

* the missing writer -- `sre_bench` now emits one root-trace record per trial,
  at the same moment and from the same numbers as the trial record that cites
  it, and the record is validated through the gate's own parser;

* #73's third site -- `verify_root_traces` failed any trial whose span tree was
  incomplete. A run that investigated and correctly took no action emits no
  approval, mutation or verification span, so its tree is incomplete by
  construction. Under the old rule every negative control failed the release
  twice: once for its own trace, and again as an "unclaimed" trace, because the
  same predicate excluded it from the claimed set.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"

from benchmarks.release_evidence import (  # noqa: E402
    ReleaseEvidenceError,
    append_root_trace,
    build_root_trace_record,
    load_root_traces,
    verify_root_traces,
)
from benchmarks.statistical_eval import (  # noqa: E402
    build_trial_record,
    load_trials,
    make_pair_id,
)
from sre_agent import trace_evidence  # noqa: E402

FINGERPRINT = "f" * 64
DIGEST = hashlib.sha256(b"trace-records").hexdigest()


def _summary(**overrides):
    """A real `trace_evidence.summary()`, so the keys the bench reads are real.

    Hand-writing this dict is how the grader record drifted from its reader:
    the producer's shape was asserted in one module and assumed in another.
    """
    summary = trace_evidence.RunTraceRecorder().summary(
        root_trace_id="trace-1",
        model_accounting={"complete": True, "cost_usd": 0.42, "cost_sources": []},
    )
    summary.update(overrides)
    return summary


def _trace(**overrides):
    payload = {
        "root_trace_id": "trace-1",
        "experiment_id": "exp-1",
        "pair_id": "p" * 64,
        "candidate_id": "full",
        "config_fingerprint": FINGERPRINT,
        "spans": 3,
        "complete": False,
        "records_sha256": DIGEST,
        "artifact_path": "reports/trace/trace-1.jsonl",
        "cost_usd": 0.42,
    }
    payload.update(overrides)
    return build_root_trace_record(**payload)


def _trial(**overrides):
    payload = {
        "experiment_id": "exp-1",
        "pair_id": "p" * 64,
        "candidate_id": "full",
        "config_fingerprint": FINGERPRINT,
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
        "trace_evidence_sha256": DIGEST,
        "trace_evidence_artifact": "reports/trace/trace-1.jsonl",
        "failure_categories": ["trace_incomplete"],
        "oracle_artifact": "reports/oracle.jsonl",
        "grader_artifact": "reports/grades.jsonl",
    }
    payload.update(overrides)
    return build_trial_record(**payload)


def _verify(traces, trials):
    return verify_root_traces(
        tuple(traces), tuple(trials), baseline_id="no_memory", candidate_id="full"
    )


def test_a_trial_that_correctly_took_no_action_does_not_fail_the_release():
    assert _verify([_trace()], [_trial()]) == []


def test_an_incomplete_trace_is_not_counted_as_unclaimed():
    reasons = _verify([_trace()], [_trial()])

    assert not [
        reason for reason in reasons if "unclaimed" in reason or "no paired" in reason
    ]


def test_a_trial_citing_no_trace_at_all_still_fails():
    reasons = _verify(
        [_trace()],
        [
            _trial(
                trace_evidence_sha256=None,
                trace_evidence_artifact=None,
                trace_span_count=0,
                cost_usd=None,
            )
        ],
    )

    assert any("records no root trace evidence" in reason for reason in reasons)


def test_a_trace_that_disagrees_about_completeness_fails():
    reasons = _verify(
        [_trace(complete=True, spans=3)], [_trial(trace_complete=False)]
    )

    assert any("disagree about whether the trace is complete" in r for r in reasons)


def test_a_trace_no_trial_claims_is_still_reported():
    reasons = _verify(
        [_trace(), _trace(root_trace_id="trace-2", records_sha256="a" * 64)],
        [_trial()],
    )

    assert any("belong to no paired trial" in reason for reason in reasons)


def test_the_producer_refuses_a_complete_trace_with_no_digest():
    with pytest.raises(ReleaseEvidenceError, match="records_sha256"):
        _trace(complete=True, records_sha256=None)


def test_the_producer_refuses_a_complete_trace_with_no_spans():
    with pytest.raises(ReleaseEvidenceError, match="records no spans"):
        _trace(complete=True, spans=0)


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_root_trace_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_the_bench_writes_a_root_trace_the_gate_can_read(bench, monkeypatch, tmp_path):
    """The round trip the schema drift kept breaking: writer, then reader."""
    summary = _summary(
        spans=3,
        records_sha256=DIGEST,
        artifact_path="reports/trace/trace-1.jsonl",
    )
    trials_path = tmp_path / "trials.jsonl"
    traces_path = tmp_path / "root-traces.jsonl"
    monkeypatch.setattr(bench, "STATISTICAL_RECORDING", True)
    monkeypatch.setattr(bench, "EXPERIMENT_ID", "exp-1")
    monkeypatch.setattr(bench, "CANDIDATE_ID", "full")
    monkeypatch.setattr(bench, "CONFIG_FINGERPRINT", FINGERPRINT)
    monkeypatch.setattr(bench, "PAIR_SEED", "seed-1")
    monkeypatch.setattr(bench, "TRIAL_RESULTS_PATH", trials_path)
    monkeypatch.setattr(bench, "ROOT_TRACE_RESULTS_PATH", traces_path)
    monkeypatch.setattr(bench, "ORACLE_RESULTS_PATH", tmp_path / "oracle.jsonl")
    monkeypatch.setattr(bench, "GRADER_RESULTS_PATH", tmp_path / "grades.jsonl")
    spec = SimpleNamespace(
        name="clean_control", scenario_version="2.0.0", risk_class="low"
    )
    score = SimpleNamespace(
        oracle_status="NO_ACTION_CORRECT",
        resolved=True,
        false_resolved=False,
        application_status="resolved",
        grader_status="PASS",
        safety_ok=True,
        mttr_seconds=None,
        structured_grade={"criteria": {"diagnosis": {"state": "PASS"}}},
    )

    bench._record_statistical_trial(
        spec,
        score,
        trial_index=1,
        latency_seconds=410.0,
        trace_completeness=summary,
    )

    traces, evidence = load_root_traces(traces_path)
    trials, _ = load_trials(trials_path)
    assert evidence.records == 1
    assert traces[0]["pair_id"] == make_pair_id(
        experiment_id="exp-1",
        dataset_sha256=bench.DATASET.sha256,
        scenario="clean_control",
        scenario_version="2.0.0",
        trial_index=1,
        pair_seed="seed-1",
    )
    assert _verify(traces, trials) == []


def test_a_run_that_recorded_no_trace_writes_no_root_trace(
    bench, monkeypatch, tmp_path
):
    """No evidence is better than invented evidence -- the gate says so itself."""
    traces_path = tmp_path / "root-traces.jsonl"
    monkeypatch.setattr(bench, "STATISTICAL_RECORDING", True)
    monkeypatch.setattr(bench, "EXPERIMENT_ID", "exp-1")
    monkeypatch.setattr(bench, "CANDIDATE_ID", "full")
    monkeypatch.setattr(bench, "CONFIG_FINGERPRINT", FINGERPRINT)
    monkeypatch.setattr(bench, "PAIR_SEED", "seed-1")
    monkeypatch.setattr(bench, "TRIAL_RESULTS_PATH", tmp_path / "trials.jsonl")
    monkeypatch.setattr(bench, "ROOT_TRACE_RESULTS_PATH", traces_path)
    monkeypatch.setattr(bench, "ORACLE_RESULTS_PATH", tmp_path / "oracle.jsonl")
    monkeypatch.setattr(bench, "GRADER_RESULTS_PATH", tmp_path / "grades.jsonl")

    bench._record_statistical_trial(
        SimpleNamespace(
            name="clean_control", scenario_version="2.0.0", risk_class="low"
        ),
        SimpleNamespace(
            oracle_status="UNRESOLVED",
            resolved=False,
            false_resolved=False,
            application_status="investigating",
            grader_status="NOT_APPLICABLE",
            safety_ok=True,
            mttr_seconds=None,
            structured_grade=None,
        ),
        trial_index=1,
        latency_seconds=12.0,
        trace_completeness=None,
    )

    assert not traces_path.exists()
