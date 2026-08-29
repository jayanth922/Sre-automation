#!/usr/bin/env python3
"""Tests for A09 content-addressed release and rollout gates."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
RELEASE_ROOT = BENCHMARKS / "release" / "v1"
MODULE_PATH = BENCHMARKS / "release_gate.py"
_spec = importlib.util.spec_from_file_location("release_gate", MODULE_PATH)
release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release
_spec.loader.exec_module(release)


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "release"
    shutil.copytree(RELEASE_ROOT, target)
    return target / "policy.json", target / "fixtures" / "safe.json"


def _rewrite(path: Path, mutate) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def test_frozen_matrix_promotes_safe_and_blocks_prompt_model_tool_regressions():
    report = release.run_matrix(RELEASE_ROOT / "ci-matrix.json")

    assert report["release_decision"] == {"status": "PASS", "reasons": []}
    assert {item["case_id"]: item["actual_status"] for item in report["cases"]} == {
        "safe-candidate-promotes": "PROMOTE",
        "regressive-prompt-blocks": "BLOCK",
        "regressive-model-blocks": "BLOCK",
        "regressive-tool-blocks": "BLOCK",
    }
    assert all(item["status"] == "PASS" for item in report["cases"])


def test_promoted_bundle_pins_policy_bundle_raw_evidence_and_rollback():
    report = release.evaluate_bundle(
        RELEASE_ROOT / "fixtures" / "safe.json",
        RELEASE_ROOT / "policy.json",
    )

    assert report["release_decision"]["status"] == "PROMOTE"
    assert len(report["policy"]["sha256"]) == 64
    assert len(report["bundle"]["sha256"]) == 64
    assert {item["kind"] for item in report["evidence_artifacts"]} == {
        "paired_trials",
        "adversarial_observations",
        "root_traces",
    }
    assert report["rollout_plan"]["initial_stage"] == "shadow"
    assert [stage["name"] for stage in report["rollout_plan"]["stages"]] == [
        "shadow",
        "canary",
    ]
    assert report["rollout_plan"]["rollback"]["automatic"] is True
    assert (
        report["rollout_plan"]["rollback"]["target_config_fingerprint"]
        == report["baseline"]["config_fingerprint"]
    )


def test_tampered_raw_artifact_is_rejected_before_release_claim(tmp_path):
    policy_path, bundle_path = _fixture_tree(tmp_path)
    (bundle_path.parent / "evidence" / "trials.jsonl").write_text(
        '{"tampered":true}\n', encoding="utf-8"
    )

    with pytest.raises(release.ReleaseGateError, match="digest mismatch"):
        release.evaluate_bundle(bundle_path, policy_path)


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("latency_seconds", 11.1, "latency exceeds"),
        ("cost_usd", 0.111, "cost exceeds"),
    ],
)
def test_approved_latency_and_cost_deltas_are_enforced(
    tmp_path, field, value, expected_reason
):
    policy_path, bundle_path = _fixture_tree(tmp_path)

    def mutate(payload):
        payload["statistical_report"]["candidate"][field]["mean"] = value

    _rewrite(bundle_path, mutate)
    report = release.evaluate_bundle(bundle_path, policy_path)

    assert report["release_decision"]["status"] == "BLOCK"
    assert any(
        expected_reason in reason for reason in report["release_decision"]["reasons"]
    )


def test_rollout_cannot_weaken_or_redirect_automatic_rollback(tmp_path):
    policy_path, bundle_path = _fixture_tree(tmp_path)

    def mutate(payload):
        rollback = payload["rollout_plan"]["rollback"]
        rollback["automatic"] = False
        rollback["target_config_fingerprint"] = "9" * 64
        rollback["triggers"]["safety_failure_count_above"] = 1

    _rewrite(bundle_path, mutate)
    report = release.evaluate_bundle(bundle_path, policy_path)
    reasons = " ".join(report["release_decision"]["reasons"])

    assert report["release_decision"]["status"] == "BLOCK"
    assert "not automatic" in reasons
    assert "not the evaluated baseline" in reasons
    assert "triggers do not match" in reasons


def test_unprotected_change_does_not_require_release_evidence(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/README.md\n", encoding="utf-8")

    report = release.evaluate_impact(
        changed,
        RELEASE_ROOT / "policy.json",
        repo_root=ROOT,
        bundle_path=None,
    )

    assert report["release_decision"]["status"] == "NOT_REQUIRED"


def test_protected_change_without_evidence_fails_closed(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text(
        "sre_agent/config/prompts/agent_base_prompt.txt\n", encoding="utf-8"
    )

    report = release.evaluate_impact(
        changed,
        RELEASE_ROOT / "policy.json",
        repo_root=ROOT,
        bundle_path=None,
    )

    assert report["release_decision"]["status"] == "BLOCK"
    assert report["protected_changes"] == ["prompt"]


def test_protected_change_requires_matching_source_digest_and_change_class(tmp_path):
    policy_path, bundle_path = _fixture_tree(tmp_path)
    repo_root = tmp_path / "repo"
    prompt = repo_root / "sre_agent" / "config" / "prompts" / "agent.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("candidate prompt\n", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("sre_agent/config/prompts/agent.txt\n", encoding="utf-8")
    policy, _ = release.load_policy(policy_path)
    source_digest = release.protected_source_digest(repo_root, policy)

    def mutate(payload):
        payload["change_class"] = "prompt"
        payload["candidate"]["source_digest"] = source_digest

    _rewrite(bundle_path, mutate)
    report = release.evaluate_impact(
        changed,
        policy_path,
        repo_root=repo_root,
        bundle_path=bundle_path,
    )

    assert report["release_decision"]["status"] == "PROMOTE"
    assert report["actual_source_digest"] == source_digest

    prompt.write_text("unevaluated prompt change\n", encoding="utf-8")
    stale = release.evaluate_impact(
        changed,
        policy_path,
        repo_root=repo_root,
        bundle_path=bundle_path,
    )
    assert stale["release_decision"]["status"] == "BLOCK"
    assert (
        "release evidence does not match protected source digest"
        in stale["release_decision"]["reasons"]
    )
