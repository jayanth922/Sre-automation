#!/usr/bin/env python3
"""Paired statistical evaluation and fail-closed promotion gates for A05."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 2
_RISK_CLASSES = {"low", "medium", "high", "critical"}
_SHA256_LENGTH = 64
_CONFIG_SECTIONS = ("provenance", "models", "tools", "runtime")


class StatisticalEvalError(ValueError):
    """Raw trial evidence is invalid or unsuitable for paired comparison."""


@dataclass(frozen=True)
class TrialRecord:
    experiment_id: str
    pair_id: str
    candidate_id: str
    config_fingerprint: str
    scenario: str
    scenario_version: str
    dataset_sha256: str
    risk_class: str
    oracle_status: str
    resolved: bool
    false_resolved: bool
    grader_status: str
    safety_ok: bool
    mttr_seconds: Optional[float]
    latency_seconds: float
    cost_usd: Optional[float]
    trace_complete: bool
    trace_span_count: int
    trace_evidence_sha256: Optional[str]
    trace_evidence_artifact: Optional[str]
    failure_categories: tuple[str, ...]
    oracle_artifact: str
    grader_artifact: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["failure_categories"] = list(self.failure_categories)
        return value

    @property
    def recovery_success(self) -> bool:
        return self.resolved and not self.false_resolved

    @property
    def quality_success(self) -> bool:
        return self.recovery_success and self.grader_status == "PASS" and self.safety_ok


@dataclass(frozen=True)
class ArtifactEvidence:
    path: str
    sha256: str
    records: int


def configuration_fingerprint(manifest: dict[str, Any]) -> str:
    """Hash only A01 configuration sections, excluding per-trial input and trace."""
    if not isinstance(manifest, dict):
        raise StatisticalEvalError("run manifest must be an object")
    missing = [section for section in _CONFIG_SECTIONS if section not in manifest]
    if missing:
        raise StatisticalEvalError(
            f"run manifest is missing configuration sections: {missing}"
        )
    configuration = {section: manifest[section] for section in _CONFIG_SECTIONS}
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def make_pair_id(
    *,
    experiment_id: str,
    dataset_sha256: str,
    scenario: str,
    scenario_version: str,
    trial_index: int,
    pair_seed: str,
) -> str:
    if trial_index < 1:
        raise StatisticalEvalError("trial_index must be positive")
    payload = {
        "experiment_id": _string(experiment_id, "experiment_id"),
        "dataset_sha256": _string(dataset_sha256, "dataset_sha256"),
        "scenario": _string(scenario, "scenario"),
        "scenario_version": _string(scenario_version, "scenario_version"),
        "trial_index": trial_index,
        "pair_seed": _string(pair_seed, "pair_seed"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_trial_schedule(
    scenario_names: list[str],
    *,
    runs_per_scenario: int,
    pair_seed: str,
    dataset_sha256: str,
    randomize: bool,
) -> tuple[tuple[str, int], ...]:
    if runs_per_scenario < 1:
        raise StatisticalEvalError("runs_per_scenario must be positive")
    if not scenario_names or len(scenario_names) != len(set(scenario_names)):
        raise StatisticalEvalError("scenario_names must be non-empty and unique")
    schedule = [
        (scenario, trial_index)
        for scenario in scenario_names
        for trial_index in range(1, runs_per_scenario + 1)
    ]
    if randomize:
        material = (
            f"{_string(pair_seed, 'pair_seed')}:"
            f"{_string(dataset_sha256, 'dataset_sha256')}"
        )
        seed = int(hashlib.sha256(material.encode()).hexdigest(), 16)
        random.Random(seed).shuffle(schedule)
    return tuple(schedule)


def build_trial_record(**values: Any) -> TrialRecord:
    """Build through the strict parser so live and offline records share validation."""
    return _parse_trial(
        {"schema_version": SCHEMA_VERSION, **values},
        line_number=1,
    )


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatisticalEvalError(f"{field} must be a non-empty string")
    return value.strip()


def _finite_number(
    value: Any, field: str, *, optional: bool = False
) -> Optional[float]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StatisticalEvalError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise StatisticalEvalError(f"{field} must be finite and non-negative")
    return parsed


def _parse_trial(payload: Any, line_number: int) -> TrialRecord:
    field = f"line {line_number}"
    if not isinstance(payload, dict):
        raise StatisticalEvalError(f"{field} must be an object")
    expected = {
        "schema_version",
        "experiment_id",
        "pair_id",
        "candidate_id",
        "config_fingerprint",
        "scenario",
        "scenario_version",
        "dataset_sha256",
        "risk_class",
        "oracle_status",
        "resolved",
        "false_resolved",
        "grader_status",
        "safety_ok",
        "mttr_seconds",
        "latency_seconds",
        "cost_usd",
        "trace_complete",
        "trace_span_count",
        "trace_evidence_sha256",
        "trace_evidence_artifact",
        "failure_categories",
        "oracle_artifact",
        "grader_artifact",
    }
    if set(payload) != expected:
        raise StatisticalEvalError(f"{field} keys do not match trial schema v2")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise StatisticalEvalError(f"{field} has unsupported schema version")
    for key in (
        "resolved",
        "false_resolved",
        "safety_ok",
        "trace_complete",
    ):
        if not isinstance(payload[key], bool):
            raise StatisticalEvalError(f"{field}.{key} must be boolean")
    if payload["resolved"] and payload["false_resolved"]:
        raise StatisticalEvalError(f"{field} cannot be resolved and false_resolved")
    oracle_status = _string(payload["oracle_status"], f"{field}.oracle_status")
    grader_status = _string(payload["grader_status"], f"{field}.grader_status")
    if oracle_status not in {
        "VERIFIED_RECOVERED",
        "UNRESOLVED",
        "INVALID_SCENARIO",
    }:
        raise StatisticalEvalError(f"{field}.oracle_status is unsupported")
    if grader_status not in {
        "PASS",
        "FAIL",
        "INCOMPLETE",
        "NOT_APPLICABLE",
    }:
        raise StatisticalEvalError(f"{field}.grader_status is unsupported")
    if payload["resolved"] and grader_status == "NOT_APPLICABLE":
        raise StatisticalEvalError(
            f"{field} resolved trial requires a structured grader outcome"
        )
    if not payload["resolved"] and grader_status != "NOT_APPLICABLE":
        raise StatisticalEvalError(
            f"{field} unresolved trial must use NOT_APPLICABLE grader status"
        )
    fingerprint = _string(payload["config_fingerprint"], f"{field}.config_fingerprint")
    dataset_sha = _string(payload["dataset_sha256"], f"{field}.dataset_sha256")
    for value, name in (
        (fingerprint, "config_fingerprint"),
        (dataset_sha, "dataset_sha256"),
    ):
        if len(value) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise StatisticalEvalError(f"{field}.{name} must be lowercase SHA-256")
    risk_class = _string(payload["risk_class"], f"{field}.risk_class")
    if risk_class not in _RISK_CLASSES:
        raise StatisticalEvalError(f"{field}.risk_class is unsupported")
    categories = payload["failure_categories"]
    if not isinstance(categories, list) or not all(
        isinstance(item, str) and item.strip() for item in categories
    ):
        raise StatisticalEvalError(f"{field}.failure_categories must be a string list")
    if len(categories) != len(set(categories)):
        raise StatisticalEvalError(f"{field}.failure_categories contains duplicates")
    latency = _finite_number(payload["latency_seconds"], f"{field}.latency_seconds")
    mttr = _finite_number(
        payload["mttr_seconds"], f"{field}.mttr_seconds", optional=True
    )
    cost = _finite_number(payload["cost_usd"], f"{field}.cost_usd", optional=True)
    trace_span_count = payload["trace_span_count"]
    if (
        isinstance(trace_span_count, bool)
        or not isinstance(trace_span_count, int)
        or trace_span_count < 0
    ):
        raise StatisticalEvalError(
            f"{field}.trace_span_count must be a non-negative integer"
        )
    trace_sha = payload["trace_evidence_sha256"]
    if trace_sha is not None:
        trace_sha = _string(trace_sha, f"{field}.trace_evidence_sha256")
        if len(trace_sha) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in trace_sha
        ):
            raise StatisticalEvalError(
                f"{field}.trace_evidence_sha256 must be lowercase SHA-256"
            )
    trace_artifact = payload["trace_evidence_artifact"]
    if trace_artifact is not None:
        trace_artifact = _string(trace_artifact, f"{field}.trace_evidence_artifact")
    if payload["trace_complete"]:
        if (
            trace_span_count < 1
            or trace_sha is None
            or trace_artifact is None
            or cost is None
        ):
            raise StatisticalEvalError(
                f"{field} complete trace requires spans, digest, artifact, and cost"
            )
    elif cost is not None:
        raise StatisticalEvalError(f"{field} incomplete trace cannot claim a cost")
    if payload["resolved"] and mttr is None:
        raise StatisticalEvalError(f"{field} resolved trial requires MTTR")
    return TrialRecord(
        experiment_id=_string(payload["experiment_id"], f"{field}.experiment_id"),
        pair_id=_string(payload["pair_id"], f"{field}.pair_id"),
        candidate_id=_string(payload["candidate_id"], f"{field}.candidate_id"),
        config_fingerprint=fingerprint,
        scenario=_string(payload["scenario"], f"{field}.scenario"),
        scenario_version=_string(
            payload["scenario_version"], f"{field}.scenario_version"
        ),
        dataset_sha256=dataset_sha,
        risk_class=risk_class,
        oracle_status=oracle_status,
        resolved=payload["resolved"],
        false_resolved=payload["false_resolved"],
        grader_status=grader_status,
        safety_ok=payload["safety_ok"],
        mttr_seconds=mttr,
        latency_seconds=float(latency),
        cost_usd=cost,
        trace_complete=payload["trace_complete"],
        trace_span_count=trace_span_count,
        trace_evidence_sha256=trace_sha,
        trace_evidence_artifact=trace_artifact,
        failure_categories=tuple(categories),
        oracle_artifact=_string(payload["oracle_artifact"], f"{field}.oracle_artifact"),
        grader_artifact=_string(payload["grader_artifact"], f"{field}.grader_artifact"),
    )


def load_trials(path: Path) -> tuple[tuple[TrialRecord, ...], ArtifactEvidence]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise StatisticalEvalError(f"trial artifact does not exist: {path}") from exc
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise StatisticalEvalError("trial artifact is empty")
    trials: list[TrialRecord] = []
    identities: set[tuple[str, str, str]] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise StatisticalEvalError(f"line {line_number} is empty")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StatisticalEvalError(f"line {line_number} is invalid JSON") from exc
        trial = _parse_trial(payload, line_number)
        identity = (trial.experiment_id, trial.pair_id, trial.candidate_id)
        if identity in identities:
            raise StatisticalEvalError(f"duplicate trial identity: {identity}")
        identities.add(identity)
        trials.append(trial)
    return tuple(trials), ArtifactEvidence(
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        records=len(trials),
    )


def append_trial(path: Path, trial: TrialRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(trial.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        )


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    if total < 1 or successes < 0 or successes > total:
        raise StatisticalEvalError("Wilson interval requires 0 <= successes <= total")
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, center - spread), min(1.0, center + spread)


def bootstrap_mean_interval(
    values: list[float],
    *,
    seed: int,
    samples: int = 4000,
    alpha: float = 0.05,
) -> tuple[float, float]:
    if not values:
        raise StatisticalEvalError("bootstrap interval requires values")
    if samples < 100:
        raise StatisticalEvalError("bootstrap requires at least 100 samples")
    if not 0 < alpha < 1:
        raise StatisticalEvalError("bootstrap alpha must be between zero and one")
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    count = len(values)
    means = sorted(
        statistics.mean(rng.choice(values) for _ in range(count))
        for _ in range(samples)
    )
    lower = means[max(0, int((alpha / 2) * samples))]
    upper = means[min(samples - 1, int((1 - alpha / 2) * samples) - 1)]
    return lower, upper


def _percentile(values: list[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _cohens_dz(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    deviation = statistics.stdev(values)
    if deviation == 0:
        return None
    return statistics.mean(values) / deviation


def pass_at_k(successes: int, total: int, k: int) -> Optional[float]:
    if k < 1:
        raise StatisticalEvalError("k must be positive")
    if total < k:
        return None
    failures = total - successes
    if failures < k:
        return 1.0
    return 1.0 - math.comb(failures, k) / math.comb(total, k)


def pass_power_k(successes: int, total: int, k: int) -> Optional[float]:
    if k < 1:
        raise StatisticalEvalError("k must be positive")
    if total < k:
        return None
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(total, k)


def _candidate_summary(trials: list[TrialRecord], k: int) -> dict[str, Any]:
    recovery = sum(trial.recovery_success for trial in trials)
    quality = sum(trial.quality_success for trial in trials)
    safety = sum(trial.safety_ok for trial in trials)
    mttrs = [
        trial.mttr_seconds
        for trial in trials
        if trial.mttr_seconds is not None and trial.recovery_success
    ]
    latencies = [trial.latency_seconds for trial in trials]
    costs = [trial.cost_usd for trial in trials if trial.cost_usd is not None]
    failures = Counter(
        category for trial in trials for category in trial.failure_categories
    )
    return {
        "candidate_id": trials[0].candidate_id,
        "config_fingerprint": trials[0].config_fingerprint,
        "runs": len(trials),
        "recovery": {
            "successes": recovery,
            "rate": recovery / len(trials),
            "wilson_95": list(wilson_interval(recovery, len(trials))),
            "pass_at_k": pass_at_k(recovery, len(trials), k),
            "pass_power_k": pass_power_k(recovery, len(trials), k),
        },
        "quality": {
            "successes": quality,
            "rate": quality / len(trials),
            "wilson_95": list(wilson_interval(quality, len(trials))),
            "pass_at_k": pass_at_k(quality, len(trials), k),
            "pass_power_k": pass_power_k(quality, len(trials), k),
        },
        "structured_complete": sum(trial.grader_status == "PASS" for trial in trials),
        "safety_rate": safety / len(trials),
        "oracle_mttr_seconds": {
            "count": len(mttrs),
            "mean": statistics.mean(mttrs) if mttrs else None,
            "median": statistics.median(mttrs) if mttrs else None,
            "p95": _percentile(mttrs, 0.95),
        },
        "latency_seconds": {
            "count": len(latencies),
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p95": _percentile(latencies, 0.95),
        },
        "cost_usd": {
            "count": len(costs),
            "coverage": len(costs) / len(trials),
            "mean": statistics.mean(costs) if costs else None,
            "p95": _percentile(costs, 0.95),
        },
        "failure_categories": dict(sorted(failures.items())),
        "failure_category_rates": {
            category: count / len(trials)
            for category, count in sorted(failures.items())
        },
    }


def _scenario_summaries(trials: list[TrialRecord], k: int) -> dict[str, dict[str, Any]]:
    scenarios = sorted({trial.scenario for trial in trials})
    return {
        scenario: _candidate_summary(
            [trial for trial in trials if trial.scenario == scenario], k
        )
        for scenario in scenarios
    }


def _paired_metric(
    baseline: list[TrialRecord],
    candidate: list[TrialRecord],
    metric,
    *,
    seed: int,
) -> dict[str, Any]:
    deltas = [
        float(metric(after)) - float(metric(before))
        for before, after in zip(baseline, candidate)
    ]
    interval = bootstrap_mean_interval(deltas, seed=seed)
    return {
        "pairs": len(deltas),
        "mean_delta": statistics.mean(deltas),
        "bootstrap_95": list(interval),
        "cohens_dz": _cohens_dz(deltas),
        "raw_deltas": deltas,
    }


def _paired_binary_metric(
    baseline: list[TrialRecord],
    candidate: list[TrialRecord],
    metric,
    *,
    seed: int,
) -> dict[str, Any]:
    result = _paired_metric(
        baseline,
        candidate,
        metric,
        seed=seed,
    )
    baseline_successes = sum(bool(metric(trial)) for trial in baseline)
    candidate_successes = sum(bool(metric(trial)) for trial in candidate)
    baseline_interval = wilson_interval(baseline_successes, len(baseline))
    candidate_interval = wilson_interval(candidate_successes, len(candidate))
    result["conservative_wilson_95"] = [
        candidate_interval[0] - baseline_interval[1],
        candidate_interval[1] - baseline_interval[0],
    ]
    return result


def compare_candidates(
    trials: tuple[TrialRecord, ...],
    *,
    baseline_id: str,
    candidate_id: str,
    artifact: ArtifactEvidence,
    minimum_pairs: int = 20,
    maximum_ci_width: float = 0.20,
    noninferiority_margin: float = 0.05,
    k: int = 3,
    bootstrap_seed: int = 1729,
) -> dict[str, Any]:
    if minimum_pairs < 1:
        raise StatisticalEvalError("minimum_pairs must be positive")
    if not 0 < maximum_ci_width <= 2:
        raise StatisticalEvalError("maximum_ci_width must be in (0, 2]")
    if not 0 <= noninferiority_margin <= 1:
        raise StatisticalEvalError("noninferiority_margin must be in [0, 1]")
    if baseline_id == candidate_id:
        raise StatisticalEvalError("baseline and candidate IDs must differ")
    selected = [
        trial for trial in trials if trial.candidate_id in {baseline_id, candidate_id}
    ]
    if not selected:
        raise StatisticalEvalError("no trials match the requested candidates")
    experiments = {trial.experiment_id for trial in selected}
    if len(experiments) != 1:
        raise StatisticalEvalError("paired comparison requires one experiment")
    fingerprints: dict[str, set[str]] = {
        baseline_id: {
            trial.config_fingerprint
            for trial in selected
            if trial.candidate_id == baseline_id
        },
        candidate_id: {
            trial.config_fingerprint
            for trial in selected
            if trial.candidate_id == candidate_id
        },
    }
    if any(len(values) != 1 for values in fingerprints.values()):
        raise StatisticalEvalError("each candidate must have one config fingerprint")
    if fingerprints[baseline_id] == fingerprints[candidate_id]:
        raise StatisticalEvalError(
            "candidate configurations have identical fingerprints"
        )

    by_candidate: dict[str, dict[str, TrialRecord]] = {
        baseline_id: {},
        candidate_id: {},
    }
    for trial in selected:
        by_candidate[trial.candidate_id][trial.pair_id] = trial
    baseline_pairs = set(by_candidate[baseline_id])
    candidate_pairs = set(by_candidate[candidate_id])
    if baseline_pairs != candidate_pairs:
        missing_baseline = sorted(candidate_pairs - baseline_pairs)
        missing_candidate = sorted(baseline_pairs - candidate_pairs)
        raise StatisticalEvalError(
            "paired trials are incomplete; "
            f"missing baseline={missing_baseline}, "
            f"missing candidate={missing_candidate}"
        )
    pair_ids = sorted(baseline_pairs)
    if not pair_ids:
        raise StatisticalEvalError("comparison has no complete pairs")
    baseline = [by_candidate[baseline_id][pair_id] for pair_id in pair_ids]
    candidate = [by_candidate[candidate_id][pair_id] for pair_id in pair_ids]
    for before, after in zip(baseline, candidate):
        if (
            before.scenario != after.scenario
            or before.scenario_version != after.scenario_version
            or before.dataset_sha256 != after.dataset_sha256
            or before.risk_class != after.risk_class
        ):
            raise StatisticalEvalError(
                f"pair {before.pair_id} does not share scenario provenance"
            )

    recovery = _paired_binary_metric(
        baseline,
        candidate,
        lambda trial: trial.recovery_success,
        seed=bootstrap_seed,
    )
    quality = _paired_binary_metric(
        baseline,
        candidate,
        lambda trial: trial.quality_success,
        seed=bootstrap_seed + 1,
    )
    latency = _paired_metric(
        baseline,
        candidate,
        lambda trial: trial.latency_seconds,
        seed=bootstrap_seed + 2,
    )
    matched_mttr = [
        index
        for index, (before, after) in enumerate(zip(baseline, candidate))
        if before.recovery_success
        and after.recovery_success
        and before.mttr_seconds is not None
        and after.mttr_seconds is not None
    ]
    mttr = (
        _paired_metric(
            [baseline[index] for index in matched_mttr],
            [candidate[index] for index in matched_mttr],
            lambda trial: trial.mttr_seconds,
            seed=bootstrap_seed + 3,
        )
        if matched_mttr
        else None
    )
    matched_cost = [
        index
        for index, (before, after) in enumerate(zip(baseline, candidate))
        if before.cost_usd is not None and after.cost_usd is not None
    ]
    cost = (
        _paired_metric(
            [baseline[index] for index in matched_cost],
            [candidate[index] for index in matched_cost],
            lambda trial: trial.cost_usd,
            seed=bootstrap_seed + 4,
        )
        if matched_cost
        else None
    )
    critical_indices = [
        index
        for index, trial in enumerate(baseline)
        if trial.risk_class in {"high", "critical"}
    ]
    critical_recovery = None
    if critical_indices:
        critical_recovery = _paired_binary_metric(
            [baseline[index] for index in critical_indices],
            [candidate[index] for index in critical_indices],
            lambda trial: trial.recovery_success,
            seed=bootstrap_seed + 5,
        )
    critical_quality = None
    if critical_indices:
        critical_quality = _paired_binary_metric(
            [baseline[index] for index in critical_indices],
            [candidate[index] for index in critical_indices],
            lambda trial: trial.quality_success,
            seed=bootstrap_seed + 6,
        )

    reasons: list[str] = []
    if len(pair_ids) < minimum_pairs:
        reasons.append(f"only {len(pair_ids)} paired trials; requires {minimum_pairs}")
    recovery_ci = recovery["conservative_wilson_95"]
    if recovery_ci[1] - recovery_ci[0] > maximum_ci_width:
        reasons.append("paired recovery uncertainty exceeds maximum CI width")
    if recovery_ci[0] < -noninferiority_margin:
        reasons.append("candidate recovery is not non-inferior")
    quality_ci = quality["conservative_wilson_95"]
    if quality_ci[1] - quality_ci[0] > maximum_ci_width:
        reasons.append("paired quality uncertainty exceeds maximum CI width")
    if quality_ci[0] < -noninferiority_margin:
        reasons.append("candidate quality is not non-inferior")
    if any(not trial.safety_ok for trial in candidate):
        reasons.append("candidate has a safety failure")
    if any(trial.grader_status != "PASS" for trial in candidate):
        reasons.append("candidate has incomplete or failed structured grades")
    if any(
        not trial.trace_complete or trial.cost_usd is None
        for trial in (*baseline, *candidate)
    ):
        reasons.append("complete root-trace cost is required for every paired trial")
    if critical_recovery is not None and critical_recovery["mean_delta"] < 0:
        reasons.append("candidate regresses a high/critical-risk slice")
    if critical_quality is not None and critical_quality["mean_delta"] < 0:
        reasons.append("candidate quality regresses a high/critical-risk slice")

    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": next(iter(experiments)),
        "baseline": {
            **_candidate_summary(baseline, k),
            "scenarios": _scenario_summaries(baseline, k),
        },
        "candidate": {
            **_candidate_summary(candidate, k),
            "scenarios": _scenario_summaries(candidate, k),
        },
        "paired": {
            "pair_count": len(pair_ids),
            "pair_ids": pair_ids,
            "recovery": recovery,
            "quality": quality,
            "latency_seconds": latency,
            "oracle_mttr_seconds": mttr,
            "cost_usd": cost,
            "critical_recovery": critical_recovery,
            "critical_quality": critical_quality,
        },
        "policy": {
            "minimum_pairs": minimum_pairs,
            "maximum_ci_width": maximum_ci_width,
            "noninferiority_margin": noninferiority_margin,
            "pass_k": k,
            "bootstrap_seed": bootstrap_seed,
        },
        "raw_artifacts": [asdict(artifact)],
        "release_decision": {
            "status": "PROMOTE" if not reasons else "BLOCK",
            "reasons": reasons,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare paired SRE benchmark candidates"
    )
    parser.add_argument("trials", type=Path, help="Raw trial JSONL")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-pairs", type=int, default=20)
    parser.add_argument("--maximum-ci-width", type=float, default=0.20)
    parser.add_argument("--noninferiority-margin", type=float, default=0.05)
    parser.add_argument("--pass-k", type=int, default=3)
    parser.add_argument("--bootstrap-seed", type=int, default=1729)
    args = parser.parse_args()
    try:
        trials, artifact = load_trials(args.trials)
        report = compare_candidates(
            trials,
            baseline_id=args.baseline,
            candidate_id=args.candidate,
            artifact=artifact,
            minimum_pairs=args.minimum_pairs,
            maximum_ci_width=args.maximum_ci_width,
            noninferiority_margin=args.noninferiority_margin,
            k=args.pass_k,
            bootstrap_seed=args.bootstrap_seed,
        )
    except StatisticalEvalError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["release_decision"], sort_keys=True))
    return 0 if report["release_decision"]["status"] == "PROMOTE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
