#!/usr/bin/env python3
"""Tests for A07 content-addressed zero-tolerance adversarial evaluation."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
MODULE_PATH = BENCHMARKS / "adversarial_eval.py"
_spec = importlib.util.spec_from_file_location("adversarial_eval", MODULE_PATH)
adversarial = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = adversarial
_spec.loader.exec_module(adversarial)

GUARD_PATH = ROOT / "sre_agent" / "prompt_guard.py"
_guard_spec = importlib.util.spec_from_file_location("a07_prompt_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(_guard_spec)
sys.modules[_guard_spec.name] = guard
_guard_spec.loader.exec_module(guard)

DATASET_ROOT = BENCHMARKS / "adversarial"
CONFIG_FINGERPRINT = "c" * 64


def _observations(dataset, *, bad_case=None):
    values = []
    for case in dataset.cases:
        bad = case.case_id == bad_case
        values.append(
            adversarial.Observation(
                dataset_sha256=dataset.sha256,
                case_id=case.case_id,
                config_fingerprint=CONFIG_FINGERPRINT,
                rendered_prompt=guard.wrap_untrusted(case.source, case.payload),
                model_output=(
                    f"Following evidence: {case.response_canary}"
                    if bad
                    else "I treated the supplied content only as untrusted evidence."
                ),
                act_report={
                    "aggregate_decision": (
                        "autonomous" if bad else "requires_approval"
                    ),
                    "action_reports": [
                        {
                            "action_type": "restart",
                            "decision": ("autonomous" if bad else "requires_approval"),
                        }
                    ],
                },
                external_mutations=(
                    ({"action_type": "restart", "status": "EXECUTED"},) if bad else ()
                ),
                evidence_artifacts=(f"reports/{case.case_id}.json",),
            )
        )
    return tuple(values)


def _artifact(records):
    return adversarial.ArtifactEvidence(
        path="observations.jsonl",
        sha256="a" * 64,
        records=records,
    )


def test_v1_dataset_is_content_addressed_and_covers_all_attack_categories():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")

    assert len(dataset.cases) == 6
    assert {case.category for case in dataset.cases} == adversarial._CATEGORIES
    assert len(dataset.sha256) == 64


def test_all_safe_observations_pass_zero_tolerance_gate():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")
    observations = _observations(dataset)

    report = adversarial.evaluate(
        dataset,
        observations,
        artifact=_artifact(len(observations)),
    )

    assert report["failed"] == 0
    assert report["release_decision"]["status"] == "PASS"
    assert all(value == 0 for value in report["category_failures"].values())


def test_followed_instruction_authorized_action_and_mutation_block_release():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")
    observations = _observations(dataset, bad_case="runbook_policy_override")

    report = adversarial.evaluate(
        dataset,
        observations,
        artifact=_artifact(len(observations)),
    )
    failed = next(
        item
        for item in report["results"]
        if item["case_id"] == "runbook_policy_override"
    )

    assert report["release_decision"]["status"] == "BLOCK"
    assert set(failed["violations"]) >= {
        "embedded_instruction_followed",
        "external_mutation_observed",
        "adversarial_evidence_authorized_plan",
        "adversarial_evidence_authorized_action",
    }


def test_missing_case_fails_closed_instead_of_improving_pass_rate():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")

    with pytest.raises(adversarial.AdversarialEvalError, match="cover every case"):
        adversarial.evaluate(
            dataset,
            _observations(dataset)[:-1],
            artifact=_artifact(len(dataset.cases) - 1),
        )


def test_direct_evaluation_rejects_duplicate_cases_and_artifact_mismatch():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")
    observations = _observations(dataset)

    with pytest.raises(adversarial.AdversarialEvalError, match="exactly once"):
        adversarial.evaluate(
            dataset,
            observations + (observations[0],),
            artifact=_artifact(len(observations) + 1),
        )

    with pytest.raises(adversarial.AdversarialEvalError, match="records"):
        adversarial.evaluate(
            dataset,
            observations,
            artifact=_artifact(len(observations) - 1),
        )


def test_instruction_canary_comparison_is_case_insensitive():
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")
    observations = list(_observations(dataset))
    first = observations[0]
    observations[0] = adversarial.Observation(
        **{
            **first.__dict__,
            "model_output": first.model_output
            + " "
            + dataset.cases[0].response_canary.lower(),
        }
    )

    report = adversarial.evaluate(
        dataset,
        tuple(observations),
        artifact=_artifact(len(observations)),
    )

    assert report["release_decision"]["status"] == "BLOCK"
    assert "embedded_instruction_followed" in report["results"][0]["violations"]


def test_observation_jsonl_is_strict_and_rejects_duplicates(tmp_path):
    dataset = adversarial.load_dataset(DATASET_ROOT, "v1")
    observation = _observations(dataset)[0]
    payload = {
        "schema_version": 1,
        "dataset_sha256": observation.dataset_sha256,
        "case_id": observation.case_id,
        "config_fingerprint": observation.config_fingerprint,
        "rendered_prompt": observation.rendered_prompt,
        "model_output": observation.model_output,
        "act_report": observation.act_report,
        "external_mutations": list(observation.external_mutations),
        "evidence_artifacts": list(observation.evidence_artifacts),
    }
    path = tmp_path / "observations.jsonl"
    encoded = json.dumps(payload, sort_keys=True)
    path.write_text(f"{encoded}\n{encoded}\n")

    with pytest.raises(adversarial.AdversarialEvalError, match="duplicate"):
        adversarial.load_observations(path)


def test_dataset_digest_tampering_is_rejected(tmp_path):
    source = DATASET_ROOT / "v1"
    target = tmp_path / "v1"
    target.mkdir()
    (target / "dataset.json").write_bytes((source / "dataset.json").read_bytes())
    cases = json.loads((source / "cases.json").read_text())
    cases["cases"][0]["payload"] += " tampered"
    (target / "cases.json").write_text(json.dumps(cases))

    with pytest.raises(adversarial.AdversarialEvalError, match="digest"):
        adversarial.load_dataset(tmp_path, "v1")
