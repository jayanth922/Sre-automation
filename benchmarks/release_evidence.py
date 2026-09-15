#!/usr/bin/env python3
"""Verify release evidence by reading the records, not the summary above them.

`release_gate.py` used to check three things about an evidence artifact: that
it exists, that its SHA-256 matches the bundle, and that it has as many lines
as the bundle claims. None of those opens a line. Every row in every shipped
bundle read `{"fixture": "paired-trials-v1", "record": 1}` — a marker with no
measurement in it — and the gate promoted on them, because the verdict it
reported came from a `release_decision` field written by hand inside the same
bundle. All four cases in the CI regression matrix shared byte-identical
evidence files while claiming four different outcomes, so the matrix proved
only that the gate could read the word BLOCK out of JSON.

The evaluators that produce real evidence already validate it far more
strictly than the gate did: `statistical_eval.load_trials` rejects any row
whose key set is not trial schema v2, and `adversarial_eval.load_observations`
does the same for observations. This module's whole job is to make the gate
use them, and then to *recompute* the reports from the records so that the
bundle's own summary becomes a claim to check rather than the source of truth.

Root traces have no published record schema, so the one below is derived
rather than invented: every field is something the trial record already
commits to (`trace_evidence_sha256`, `trace_evidence_artifact`,
`trace_span_count`), and verification is the two agreeing. A trial that
claims a complete trace must point at a trace record that exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from benchmarks.adversarial_eval import (
    AdversarialEvalError,
    load_dataset,
    load_observations,
)
from benchmarks.adversarial_eval import evaluate as evaluate_adversarial
from benchmarks.statistical_eval import (
    ArtifactEvidence,
    StatisticalEvalError,
    TrialRecord,
    compare_candidates,
    load_trials,
)

ROOT_TRACE_SCHEMA_VERSION = 1
_SHA256_LENGTH = 64
_ROOT_TRACE_KEYS = {
    "schema_version",
    "root_trace_id",
    "experiment_id",
    "pair_id",
    "candidate_id",
    "config_fingerprint",
    "spans",
    "complete",
    "records_sha256",
    "artifact_path",
    "cost_usd",
}


class ReleaseEvidenceError(ValueError):
    """An evidence artifact does not contain the records it is offered as."""


def _sha256_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReleaseEvidenceError(f"{field} must be lowercase SHA-256")
    return value


def _parse_root_trace(payload: Any, line_number: int) -> dict[str, Any]:
    field = f"root trace line {line_number}"
    if not isinstance(payload, dict):
        raise ReleaseEvidenceError(f"{field} must be an object")
    if set(payload) != _ROOT_TRACE_KEYS:
        raise ReleaseEvidenceError(
            f"{field} keys do not match root-trace schema "
            f"v{ROOT_TRACE_SCHEMA_VERSION}"
        )
    if payload["schema_version"] != ROOT_TRACE_SCHEMA_VERSION:
        raise ReleaseEvidenceError(f"{field} has an unsupported schema version")
    for key in ("root_trace_id", "experiment_id", "pair_id", "candidate_id"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ReleaseEvidenceError(f"{field}.{key} must be a non-empty string")
    _sha256_text(payload["config_fingerprint"], f"{field}.config_fingerprint")
    if not isinstance(payload["complete"], bool):
        raise ReleaseEvidenceError(f"{field}.complete must be boolean")
    spans = payload["spans"]
    if isinstance(spans, bool) or not isinstance(spans, int) or spans < 0:
        raise ReleaseEvidenceError(f"{field}.spans must be a non-negative integer")
    if payload["complete"]:
        # An incomplete trace is allowed to be missing its digest and cost;
        # a complete one claiming to be evidence is not.
        _sha256_text(payload["records_sha256"], f"{field}.records_sha256")
        if not isinstance(payload["artifact_path"], str) or not payload[
            "artifact_path"
        ].strip():
            raise ReleaseEvidenceError(f"{field}.artifact_path must be a path")
        if spans < 1:
            raise ReleaseEvidenceError(f"{field} complete trace records no spans")
        cost = payload["cost_usd"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise ReleaseEvidenceError(f"{field}.cost_usd must be a non-negative number")
    return payload


def load_root_traces(path: Path) -> tuple[tuple[dict[str, Any], ...], ArtifactEvidence]:
    """Parse root-trace evidence, refusing anything that is not a trace record."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ReleaseEvidenceError(f"root-trace artifact does not exist: {path}") from exc
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise ReleaseEvidenceError("root-trace artifact is empty")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ReleaseEvidenceError(f"root-trace line {line_number} is empty")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReleaseEvidenceError(
                f"root-trace line {line_number} is invalid JSON"
            ) from exc
        record = _parse_root_trace(payload, line_number)
        identity = record["root_trace_id"]
        if identity in seen:
            raise ReleaseEvidenceError(f"duplicate root trace: {identity}")
        seen.add(identity)
        records.append(record)
    return tuple(records), ArtifactEvidence(
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        records=len(records),
    )


def recompute_statistical_report(
    trials_path: Path,
    *,
    baseline_id: str,
    candidate_id: str,
    statistical_policy: dict[str, Any],
) -> tuple[dict[str, Any], tuple[TrialRecord, ...]]:
    """Re-derive the paired comparison from the trial records themselves.

    The policy states a separate non-inferiority margin for recovery and for
    quality; `compare_candidates` applies one margin to both. Handing it the
    stricter of the two can only make its own verdict harder to pass, and the
    gate still applies each metric's own margin to the recomputed interval
    afterwards, so neither margin is loosened here.
    """
    try:
        trials, artifact = load_trials(trials_path)
        report = compare_candidates(
            trials,
            baseline_id=baseline_id,
            candidate_id=candidate_id,
            artifact=artifact,
            minimum_pairs=statistical_policy["minimum_pairs"],
            maximum_ci_width=statistical_policy["maximum_ci_width"],
            noninferiority_margin=min(
                statistical_policy["recovery_noninferiority_margin"],
                statistical_policy["quality_noninferiority_margin"],
            ),
        )
    except StatisticalEvalError as exc:
        raise ReleaseEvidenceError(f"paired-trial evidence is not usable: {exc}") from exc
    return report, trials


def resolve_dataset(dataset_root: Path, dataset_version: str):
    """Find the checked-in dataset that declares `dataset_version`.

    `load_dataset` is addressed by directory name (`v1`), while the version
    a report names is the one declared inside it (`sentinel-adversarial-v1`).
    A bundle should cite the version it was evaluated against, not the
    directory layout of the repository that held it, so the directory is
    looked up rather than assumed.
    """
    matches = []
    for directory in sorted(path for path in dataset_root.iterdir() if path.is_dir()):
        if not (directory / "dataset.json").exists():
            continue
        try:
            dataset = load_dataset(dataset_root, directory.name)
        except AdversarialEvalError as exc:
            raise ReleaseEvidenceError(
                f"adversarial dataset {directory.name} is not usable: {exc}"
            ) from exc
        if dataset.version == dataset_version:
            matches.append(dataset)
    if not matches:
        raise ReleaseEvidenceError(
            f"no checked-in adversarial dataset declares version {dataset_version!r}"
        )
    if len(matches) > 1:
        raise ReleaseEvidenceError(
            f"adversarial dataset version {dataset_version!r} is ambiguous"
        )
    return matches[0]


def recompute_adversarial_report(
    observations_path: Path,
    *,
    dataset_root: Path,
    dataset_version: str,
) -> dict[str, Any]:
    """Re-derive the adversarial verdict by replaying the checked-in cases."""
    dataset = resolve_dataset(dataset_root, dataset_version)
    try:
        observations, artifact = load_observations(observations_path)
        return evaluate_adversarial(dataset, observations, artifact=artifact)
    except AdversarialEvalError as exc:
        raise ReleaseEvidenceError(
            f"adversarial evidence is not usable: {exc}"
        ) from exc


def verify_root_traces(
    traces: tuple[dict[str, Any], ...],
    trials: tuple[TrialRecord, ...],
    *,
    baseline_id: str,
    candidate_id: str,
) -> list[str]:
    """Every trial claiming a complete trace must point at a trace that exists.

    Without this the `root_traces` artifact is decoration: the gate counted
    its lines and never asked whether any of them was the trace a trial cited
    as the source of its cost and latency.
    """
    reasons: list[str] = []
    by_digest = {
        record["records_sha256"]: record
        for record in traces
        if record["records_sha256"] is not None
    }
    paired = [trial for trial in trials if trial.candidate_id in {baseline_id, candidate_id}]
    for trial in paired:
        if not trial.trace_complete:
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} has an incomplete root trace"
            )
            continue
        record = by_digest.get(trial.trace_evidence_sha256)
        if record is None:
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} cites a root trace "
                "that is not in the evidence"
            )
            continue
        if record["spans"] != trial.trace_span_count:
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} span count "
                "disagrees with its root trace"
            )
        if record["artifact_path"] != trial.trace_evidence_artifact:
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} names a different "
                "trace artifact than the trace record"
            )
        if (
            record["pair_id"] != trial.pair_id
            or record["candidate_id"] != trial.candidate_id
            or record["config_fingerprint"] != trial.config_fingerprint
            or record["experiment_id"] != trial.experiment_id
        ):
            reasons.append(
                f"root trace {record['root_trace_id']} belongs to a different trial"
            )
        if not _close(record["cost_usd"], trial.cost_usd):
            # The trial's cost is supposed to be read off this trace. If the
            # two disagree, one of them was typed rather than measured.
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} reports a cost its "
                "root trace does not"
            )
        if not record["complete"]:
            reasons.append(
                f"trial {trial.pair_id}/{trial.candidate_id} claims a complete "
                "trace that the trace record calls incomplete"
            )
    unclaimed = len(traces) - len(
        {
            trial.trace_evidence_sha256
            for trial in paired
            if trial.trace_complete and trial.trace_evidence_sha256 in by_digest
        }
    )
    if unclaimed > 0:
        reasons.append(
            f"{unclaimed} root trace(s) belong to no paired trial in this bundle"
        )
    return list(dict.fromkeys(reasons))


def _at(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _close(left: Any, right: Any) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _close(one, other) for one, other in zip(left, right)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, bool) or isinstance(right, bool):
            return left is right
        # The bundle carries rounded numbers; the tolerance absorbs the
        # rounding and nothing wider. A different set of trials cannot
        # slip through 1e-9.
        return abs(float(left) - float(right)) <= 1e-9
    return left == right


def claim_disagreements(
    claimed: dict[str, Any],
    recomputed: dict[str, Any],
    *,
    label: str,
    paths: tuple[str, ...],
) -> list[str]:
    """Where the bundle's own summary contradicts its records, say so.

    A summary that disagrees with the evidence underneath it is the failure
    this module exists for: it means someone wrote a verdict rather than
    measuring one. The gate uses the recomputed value either way; this turns
    the discrepancy into a reason the release is blocked instead of a silent
    correction.
    """
    reasons: list[str] = []
    for path in paths:
        stated = _at(claimed, path)
        actual = _at(recomputed, path)
        if stated is None and actual is None:
            continue
        if not _close(stated, actual):
            reasons.append(
                f"{label} report claims {path}={stated!r} but its records give {actual!r}"
            )
    return reasons


def artifact_evidence_dict(artifact: ArtifactEvidence) -> dict[str, Any]:
    return asdict(artifact)


def trials_fingerprints(
    trials: tuple[TrialRecord, ...], candidate_id: str
) -> Optional[str]:
    """The single config fingerprint every trial of one arm must share."""
    values = {
        trial.config_fingerprint
        for trial in trials
        if trial.candidate_id == candidate_id
    }
    return values.pop() if len(values) == 1 else None
