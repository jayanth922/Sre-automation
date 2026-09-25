#!/usr/bin/env python3
"""The release gate has to read the evidence, not the summary sitting on it.

Defect #39, found by probing the shipped bundles rather than trusting them:

* Every line of every evidence artifact in the repository was the marker row
  `{"fixture": "paired-trials-v1", "record": 1}` — 86 of them across three
  files. The gate checked that each artifact existed, hashed to the declared
  digest, and had the declared number of lines. It never opened a line.
* Because of that, all four cases in the CI regression matrix pointed at
  byte-identical evidence while claiming four different verdicts. The matrix
  demonstrated that the gate can read the word BLOCK out of JSON.
* The verdict itself came from a `release_decision` object hand-written
  inside the same bundle the gate was judging.
* Patching one field — `candidate.source_digest` — in a copy of the shipped
  candidate bundle turned a real change to `mcp_tool_wrapper.py` and
  `agent_nodes.py` from BLOCK into `{"reasons": [], "status": "PROMOTE"}`,
  with the evidence files untouched.

The validators needed to catch all of this already existed;
`statistical_eval.load_trials` rejects any row whose key set is not trial
the current trial schema and `adversarial_eval.load_observations` does the same for
observations. The gate's mistake was `json.loads`.

There is a second, quieter half. The shipped policy demanded twenty paired
trials and a 0.05 non-inferiority margin, and the evaluator builds its
interval by subtracting two independent Wilson intervals. Twenty pairs
cannot produce an interval narrower than +/-0.161 even when both arms are
perfect, so no honest evaluation could ever have satisfied that policy.
Fabricated evidence was not a shortcut past the gate; it was the only input
the gate would accept.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import release_evidence, release_gate

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "evals" / "benchmarks" / "release" / "v1"
FIXTURE_NAMES = ("safe", "regressive-prompt", "regressive-model", "regressive-tool")


@pytest.fixture
def tree(tmp_path):
    copy = tmp_path / "release"
    shutil.copytree(RELEASE, copy)
    return copy


def _evidence_path(bundle_path: Path, kind: str) -> Path:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    entry = next(item for item in bundle["evidence_artifacts"] if item["kind"] == kind)
    return bundle_path.parent / entry["path"]


def _reseal(bundle_path: Path, kind: str) -> None:
    """Re-declare the digest and line count after editing an artifact.

    Without this a test would only prove the digest check still works, which
    was never the broken part.
    """
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    entry = next(item for item in bundle["evidence_artifacts"] if item["kind"] == kind)
    raw = (bundle_path.parent / entry["path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    entry["sha256"] = digest
    entry["records"] = len(raw.decode("utf-8").splitlines())
    report = {
        "paired_trials": "statistical_report",
        "adversarial_observations": "adversarial_report",
    }.get(kind)
    if report:
        for artifact in bundle[report]["raw_artifacts"]:
            artifact["sha256"] = digest
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _decide(tree: Path, name: str = "safe"):
    report = release_gate.evaluate_bundle(
        tree / "fixtures" / f"{name}.json", tree / "policy.json"
    )
    return report["release_decision"]["status"], report["release_decision"]["reasons"]


# ---------------------------------------------------------------------------
# The shipped fixtures decide on their records
# ---------------------------------------------------------------------------


def test_the_four_fixtures_no_longer_share_one_evidence_file():
    """The finding that made #39 worse than it was reported.

    Four cases claiming four different verdicts were reading the same three
    files, so the matrix could not have detected a regression in any of them.
    """
    digests = {}
    for name in FIXTURE_NAMES:
        bundle = json.loads((RELEASE / "fixtures" / f"{name}.json").read_text("utf-8"))
        digests[name] = tuple(
            item["sha256"]
            for item in sorted(
                bundle["evidence_artifacts"], key=lambda entry: entry["kind"]
            )
        )

    assert len(set(digests.values())) == len(FIXTURE_NAMES), digests


@pytest.mark.parametrize(
    ("name", "expected", "because"),
    [
        ("safe", "PROMOTE", None),
        ("regressive-prompt", "BLOCK", "candidate structured grades are incomplete"),
        (
            "regressive-model",
            "BLOCK",
            "candidate cost exceeds approved regression ratio",
        ),
        ("regressive-tool", "BLOCK", "candidate has an adversarial safety failure"),
    ],
)
def test_each_fixture_decides_for_its_own_recorded_reason(
    tree, name, expected, because
):
    status, reasons = _decide(tree, name)

    assert status == expected, reasons
    if because is not None:
        assert because in reasons, reasons


def test_the_regressive_model_fixture_blocks_where_the_evaluator_would_not(tree):
    """Its records carry no recovery, quality, or safety regression at all.

    The statistical evaluator promotes it; only the gate's own latency and
    cost ratios stop it. That path had no fixture exercising it before.
    """
    bundle = json.loads(
        (tree / "fixtures" / "regressive-model.json").read_text("utf-8")
    )
    assert bundle["statistical_report"]["release_decision"]["status"] == "PROMOTE"

    status, reasons = _decide(tree, "regressive-model")

    assert status == "BLOCK"
    assert reasons == [
        "candidate latency exceeds approved regression ratio",
        "candidate cost exceeds approved regression ratio",
    ]


# ---------------------------------------------------------------------------
# Marker rows: the exact content the gate used to certify
# ---------------------------------------------------------------------------


def test_the_marker_rows_the_gate_used_to_promote_are_now_refused(tree):
    path = _evidence_path(tree / "fixtures" / "safe.json", "paired_trials")
    path.write_text('{"fixture":"paired-trials-v1","record":1}\n' * 160, "utf-8")
    _reseal(tree / "fixtures" / "safe.json", "paired_trials")

    with pytest.raises(release_gate.ReleaseGateError, match="trial schema v3"):
        _decide(tree)


def test_marker_observations_are_refused_too(tree):
    path = _evidence_path(tree / "fixtures" / "safe.json", "adversarial_observations")
    path.write_text('{"fixture":"adversarial-v1","record":1}\n' * 6, "utf-8")
    _reseal(tree / "fixtures" / "safe.json", "adversarial_observations")

    with pytest.raises(release_gate.ReleaseGateError, match="observation v1"):
        _decide(tree)


def test_marker_traces_are_refused_too(tree):
    path = _evidence_path(tree / "fixtures" / "safe.json", "root_traces")
    path.write_text('{"fixture":"root-traces-v1","record":1}\n' * 160, "utf-8")
    _reseal(tree / "fixtures" / "safe.json", "root_traces")

    with pytest.raises(release_gate.ReleaseGateError, match="root-trace schema"):
        _decide(tree)


def test_an_unparseable_artifact_is_a_gate_error_not_a_traceback(tree):
    """Malformed evidence and a bad digest are the same kind of failure, so
    a caller that already handles one does not need a second except clause."""
    path = _evidence_path(tree / "fixtures" / "safe.json", "paired_trials")
    path.write_text("{not json at all\n", "utf-8")
    _reseal(tree / "fixtures" / "safe.json", "paired_trials")

    with pytest.raises(release_gate.ReleaseGateError):
        _decide(tree)


def test_a_renamed_artifact_still_has_its_records_counted(tree):
    """The strict parse and the line-count check used to be gated on a
    `.jsonl` suffix, so renaming the file turned `records` into an
    unverified assertion — while `records` still fed the pair-count,
    trace-count and adversarial-case checks."""
    bundle_path = tree / "fixtures" / "safe.json"
    bundle = json.loads(bundle_path.read_text("utf-8"))
    entry = next(
        item for item in bundle["evidence_artifacts"] if item["kind"] == "paired_trials"
    )
    source = bundle_path.parent / entry["path"]
    renamed = source.with_suffix(".txt")
    source.rename(renamed)
    entry["path"] = entry["path"].replace(".jsonl", ".txt")
    entry["records"] = 9999
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(release_gate.ReleaseGateError, match="record count"):
        _decide(tree)


# ---------------------------------------------------------------------------
# A summary that disagrees with its records is a reason to block
# ---------------------------------------------------------------------------


def test_a_hand_edited_verdict_is_reported_rather_than_obeyed(tree):
    bundle_path = tree / "fixtures" / "regressive-prompt.json"
    bundle = json.loads(bundle_path.read_text("utf-8"))
    bundle["statistical_report"]["release_decision"] = {
        "status": "PROMOTE",
        "reasons": [],
    }
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    status, reasons = _decide(tree, "regressive-prompt")

    assert status == "BLOCK"
    assert any("release_decision.status" in reason for reason in reasons), reasons
    assert "candidate structured grades are incomplete" in reasons


def test_a_hand_edited_diagnosis_metric_is_recomputed_from_trial_rows(tree):
    bundle_path = tree / "fixtures" / "safe.json"
    bundle = json.loads(bundle_path.read_text("utf-8"))
    bundle["statistical_report"]["paired"]["diagnosis"]["metric_version"] = 99
    bundle["statistical_report"]["paired"]["diagnosis"]["conservative_wilson_95"] = [
        -1.0,
        -1.0,
    ]
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    status, reasons = _decide(tree)

    assert status == "BLOCK"
    assert any(
        "paired.diagnosis.conservative_wilson_95" in reason for reason in reasons
    ), reasons
    assert any(
        "paired.diagnosis.metric_version" in reason for reason in reasons
    ), reasons


def test_an_adversarial_pass_claimed_over_a_leaking_record_is_caught(tree):
    bundle_path = tree / "fixtures" / "regressive-tool.json"
    bundle = json.loads(bundle_path.read_text("utf-8"))
    bundle["adversarial_report"]["failed"] = 0
    bundle["adversarial_report"]["passed"] = 6
    bundle["adversarial_report"]["release_decision"]["status"] = "PASS"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    status, reasons = _decide(tree, "regressive-tool")

    assert status == "BLOCK"
    assert any(
        "adversarial report claims failed=0" in reason for reason in reasons
    ), reasons
    assert "candidate has an adversarial safety failure" in reasons


# ---------------------------------------------------------------------------
# Root traces are the source of cost and latency, so they have to exist
# ---------------------------------------------------------------------------


def test_a_trial_citing_a_trace_that_is_not_in_the_evidence_blocks(tree):
    path = _evidence_path(tree / "fixtures" / "safe.json", "root_traces")
    lines = path.read_text("utf-8").splitlines()
    path.write_text("".join(line + "\n" for line in lines[:-1]), "utf-8")
    _reseal(tree / "fixtures" / "safe.json", "root_traces")

    status, reasons = _decide(tree)

    assert status == "BLOCK"
    assert any("cites a root trace that is not in the evidence" in r for r in reasons)


def test_a_trace_that_belongs_to_no_trial_blocks(tree):
    path = _evidence_path(tree / "fixtures" / "safe.json", "root_traces")
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    stray = dict(rows[0])
    stray["root_trace_id"] = "stray-trace"
    stray["records_sha256"] = "f" * 64
    rows.append(stray)
    path.write_text(
        "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows
        ),
        "utf-8",
    )
    _reseal(tree / "fixtures" / "safe.json", "root_traces")

    status, reasons = _decide(tree)

    assert status == "BLOCK"
    assert any("belong to no paired trial" in reason for reason in reasons), reasons


def test_a_trace_whose_cost_disagrees_with_its_trial_blocks(tree):
    """The trial's cost is supposed to be read off the trace. If the two
    disagree, at least one of them was typed."""
    path = _evidence_path(tree / "fixtures" / "safe.json", "root_traces")
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    rows[1]["cost_usd"] = rows[1]["cost_usd"] + 0.5
    path.write_text(
        "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows
        ),
        "utf-8",
    )
    _reseal(tree / "fixtures" / "safe.json", "root_traces")

    status, reasons = _decide(tree)

    assert status == "BLOCK"
    assert any("reports a cost its root trace does not" in r for r in reasons), reasons


# ---------------------------------------------------------------------------
# A policy nobody could satisfy honestly
# ---------------------------------------------------------------------------


def test_the_shipped_policy_is_reachable_by_a_perfect_candidate():
    policy, _ = release_gate.load_policy(RELEASE / "policy.json")
    statistical = policy["statistical"]

    for key in ("recovery_noninferiority_margin", "quality_noninferiority_margin"):
        assert statistical["minimum_pairs"] >= release_gate._reachable_pairs(
            statistical[key]
        )


def test_the_original_twenty_pair_policy_is_now_refused(tree):
    """Twenty pairs and a 0.05 margin is what shipped, and it is arithmetic
    that no evaluation could satisfy. The gate says so instead of quietly
    accepting only fabricated reports."""
    policy_path = tree / "policy.json"
    policy = json.loads(policy_path.read_text("utf-8"))
    policy["statistical"]["minimum_pairs"] = 20
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", "utf-8")

    with pytest.raises(release_gate.ReleaseGateError, match="unreachable"):
        release_gate.load_policy(policy_path)


def test_the_policy_has_to_state_its_own_confidence_interval_width(tree):
    """Otherwise the gate's answer depends on the evaluator's default."""
    policy_path = tree / "policy.json"
    policy = json.loads(policy_path.read_text("utf-8"))
    del policy["statistical"]["maximum_ci_width"]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", "utf-8")

    with pytest.raises(release_gate.ReleaseGateError, match="policy.statistical keys"):
        release_gate.load_policy(policy_path)


# ---------------------------------------------------------------------------
# The fixtures are generated, so they can be regenerated
# ---------------------------------------------------------------------------


def test_the_checked_in_fixtures_match_a_fresh_generation():
    """If this fails, someone edited a fixture by hand — which is how the
    old ones came to say things their records did not."""
    result = subprocess.run(
        [sys.executable, "-m", "benchmarks.make_release_fixtures", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_the_dataset_directory_is_found_by_the_version_a_report_names():
    dataset = release_evidence.resolve_dataset(
        ROOT / "evals" / "benchmarks" / "adversarial", "sentinel-adversarial-v1"
    )

    assert dataset.version == "sentinel-adversarial-v1"
    assert len(dataset.cases) == 6


def test_an_unknown_dataset_version_is_refused():
    with pytest.raises(release_evidence.ReleaseEvidenceError, match="no checked-in"):
        release_evidence.resolve_dataset(
            ROOT / "evals" / "benchmarks" / "adversarial", "sentinel-adversarial-v99"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
