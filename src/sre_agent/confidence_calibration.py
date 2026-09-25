#!/usr/bin/env python3
"""Task-specific confidence calibration with fail-closed runtime artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

RECORD_SCHEMA_VERSION = 2
ARTIFACT_SCHEMA_VERSION = 2
SCHEMA_VERSION = RECORD_SCHEMA_VERSION
Task = Literal["diagnosis", "remediation"]
_TASKS = {"diagnosis", "remediation"}

EvidenceSource = Literal["live_benchmark", "replay", "synthetic"]
_EVIDENCE_SOURCES = {"live_benchmark", "replay", "synthetic"}
# Only observations taken from a live benchmark run against a real workload may
# unlock autonomy. Replayed and synthetic corpora still build an artifact — they
# are how the pipeline is developed and tested — but that artifact carries no
# threshold, so the runtime keeps requiring human approval.
_AUTONOMY_EVIDENCE = "live_benchmark"

SELECTION_RULE = (
    "lowest expected cost per action among operating points whose autonomous "
    "population meets minimum_threshold_support and whose Wilson lower bound "
    "meets required_wilson_lower; ties resolve to the higher threshold"
)


class ConfidenceCalibrationError(ValueError):
    """Confidence evidence or a calibration artifact is invalid."""


@dataclass(frozen=True)
class ConfidenceRecord:
    task: Task
    rubric_version: str
    raw_confidence: float
    outcome: bool
    scenario: str
    scenario_version: str
    dataset_sha256: str
    config_fingerprint: str
    pair_id: str
    observed_at: datetime
    evidence_source: EvidenceSource = "live_benchmark"
    schema_version: int = RECORD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observed_at"] = self.observed_at.isoformat()
        return value


@dataclass(frozen=True)
class ReliabilityBin:
    lower_bound: float
    upper_bound: float
    count: int
    mean_confidence: float
    accuracy: float
    calibration_gap: float


@dataclass(frozen=True)
class ReliabilityReport:
    task: Task
    rubric_version: str
    config_fingerprint: Optional[str]
    samples: int
    bins: tuple[ReliabilityBin, ...]
    brier_score: float
    log_loss: float
    expected_calibration_error: float
    maximum_calibration_error: float
    mean_confidence: float
    outcome_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "bins": [asdict(item) for item in self.bins],
        }


@dataclass(frozen=True)
class CalibrationBin:
    upper_bound: float
    count: int
    successes: int
    calibrated_probability: float


@dataclass(frozen=True)
class CostModel:
    """What being wrong costs, in whatever unit the operator cares to declare.

    Only the ratio matters. `abstention_cost` is one human approval round trip;
    `false_autonomy_cost` is one wrong action taken without one.
    """

    false_autonomy_cost: float
    abstention_cost: float

    def action_cost(self, *, autonomous: bool, outcome: bool) -> float:
        if not autonomous:
            return self.abstention_cost
        return 0.0 if outcome else self.false_autonomy_cost


@dataclass(frozen=True)
class OperatingPoint:
    """One candidate autonomy threshold, scored on the evidence behind it."""

    threshold: float
    autonomous: int
    abstained: int
    true_autonomy: int
    false_autonomy: int
    autonomous_success_rate: float
    wilson_lower: float
    coverage: float
    expected_cost_per_action: float
    meets_support_floor: bool
    meets_wilson_floor: bool

    @property
    def eligible(self) -> bool:
        return self.meets_support_floor and self.meets_wilson_floor


@dataclass(frozen=True)
class CalibrationArtifact:
    artifact_version: str
    task: Task
    rubric_version: str
    config_fingerprint: str
    source_sha256: str
    sample_count: int
    bins: tuple[CalibrationBin, ...]
    autonomy_threshold: Optional[float]
    threshold_support: int
    threshold_wilson_lower: Optional[float]
    required_wilson_lower: float
    minimum_threshold_support: int
    cost_model: CostModel
    threshold_curve: tuple[OperatingPoint, ...]
    always_abstain_cost: float
    always_autonomous_cost: float
    selected_cost: Optional[float]
    evidence_sources: tuple[str, ...]
    autonomy_blocked_reason: Optional[str]
    built_at: datetime
    artifact_sha256: str
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    @property
    def autonomy_beats_abstention(self) -> bool:
        """Is taking the threshold cheaper than sending everything to a human?"""
        return (
            self.selected_cost is not None
            and self.selected_cost < self.always_abstain_cost
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_version": self.artifact_version,
            "task": self.task,
            "rubric_version": self.rubric_version,
            "config_fingerprint": self.config_fingerprint,
            "source_sha256": self.source_sha256,
            "sample_count": self.sample_count,
            "bins": [asdict(item) for item in self.bins],
            "autonomy_threshold": self.autonomy_threshold,
            "threshold_support": self.threshold_support,
            "threshold_wilson_lower": self.threshold_wilson_lower,
            "required_wilson_lower": self.required_wilson_lower,
            "minimum_threshold_support": self.minimum_threshold_support,
            "cost_model": asdict(self.cost_model),
            "threshold_curve": [asdict(item) for item in self.threshold_curve],
            "always_abstain_cost": self.always_abstain_cost,
            "always_autonomous_cost": self.always_autonomous_cost,
            "selected_cost": self.selected_cost,
            "evidence_sources": list(self.evidence_sources),
            "autonomy_blocked_reason": self.autonomy_blocked_reason,
            "selection_rule": SELECTION_RULE,
            "built_at": self.built_at.isoformat(),
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class CalibratedConfidence:
    task: Task
    raw_confidence: float
    calibrated_probability: float
    artifact_version: str
    artifact_sha256: str
    autonomy_threshold: Optional[float]
    autonomy_blocked_reason: Optional[str] = None

    @property
    def autonomy_eligible(self) -> bool:
        return (
            self.autonomy_threshold is not None
            and self.calibrated_probability >= self.autonomy_threshold
        )


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfidenceCalibrationError(f"{field} must be a non-empty string")
    return value.strip()


def _probability(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfidenceCalibrationError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ConfidenceCalibrationError(f"{field} must be in [0, 1]")
    return parsed


def _timestamp(value: Any, field: str) -> datetime:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfidenceCalibrationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ConfidenceCalibrationError(f"{field} must be timezone-aware")
    return parsed


def _sha256(value: Any, field: str) -> str:
    text = _string(value, field)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ConfidenceCalibrationError(f"{field} must be lowercase SHA-256")
    return text


def build_confidence_record(**values: Any) -> ConfidenceRecord:
    payload = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "evidence_source": "live_benchmark",
        **values,
    }
    expected = {
        "schema_version",
        "task",
        "rubric_version",
        "raw_confidence",
        "outcome",
        "scenario",
        "scenario_version",
        "dataset_sha256",
        "config_fingerprint",
        "pair_id",
        "observed_at",
        "evidence_source",
    }
    if set(payload) != expected:
        raise ConfidenceCalibrationError(
            "confidence record keys do not match schema v2"
        )
    if payload["schema_version"] != RECORD_SCHEMA_VERSION:
        raise ConfidenceCalibrationError("unsupported confidence record schema")
    task = _string(payload["task"], "task")
    if task not in _TASKS:
        raise ConfidenceCalibrationError(f"unsupported confidence task: {task}")
    evidence_source = _string(payload["evidence_source"], "evidence_source")
    if evidence_source not in _EVIDENCE_SOURCES:
        raise ConfidenceCalibrationError(
            f"unsupported evidence source: {evidence_source}"
        )
    if not isinstance(payload["outcome"], bool):
        raise ConfidenceCalibrationError("outcome must be boolean")
    observed_at = payload["observed_at"]
    if isinstance(observed_at, datetime):
        if observed_at.tzinfo is None:
            raise ConfidenceCalibrationError("observed_at must be timezone-aware")
        parsed_time = observed_at
    else:
        parsed_time = _timestamp(observed_at, "observed_at")
    return ConfidenceRecord(
        task=task,
        rubric_version=_string(payload["rubric_version"], "rubric_version"),
        raw_confidence=_probability(payload["raw_confidence"], "raw_confidence"),
        outcome=payload["outcome"],
        scenario=_string(payload["scenario"], "scenario"),
        scenario_version=_string(payload["scenario_version"], "scenario_version"),
        dataset_sha256=_sha256(payload["dataset_sha256"], "dataset_sha256"),
        config_fingerprint=_sha256(payload["config_fingerprint"], "config_fingerprint"),
        pair_id=_string(payload["pair_id"], "pair_id"),
        observed_at=parsed_time,
        evidence_source=evidence_source,  # type: ignore[arg-type]
    )


def append_confidence_record(path: Path, record: ConfidenceRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        )


def load_confidence_records(
    path: Path,
) -> tuple[tuple[ConfidenceRecord, ...], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfidenceCalibrationError(
            f"confidence record file does not exist: {path}"
        ) from exc
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise ConfidenceCalibrationError("confidence record file is empty")
    records: list[ConfidenceRecord] = []
    identities: set[tuple[str, str, str]] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ConfidenceCalibrationError(f"line {line_number} is empty")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfidenceCalibrationError(
                f"line {line_number} is invalid JSON"
            ) from exc
        try:
            record = build_confidence_record(**payload)
        except ConfidenceCalibrationError as exc:
            raise ConfidenceCalibrationError(f"line {line_number}: {exc}") from exc
        identity = (record.task, record.pair_id, record.config_fingerprint)
        if identity in identities:
            raise ConfidenceCalibrationError(f"duplicate confidence record: {identity}")
        identities.add(identity)
        records.append(record)
    return tuple(records), hashlib.sha256(raw).hexdigest()


def _task_records(
    records: tuple[ConfidenceRecord, ...],
    task: str,
    config_fingerprint: Optional[str] = None,
) -> list[ConfidenceRecord]:
    if task not in _TASKS:
        raise ConfidenceCalibrationError(f"unsupported confidence task: {task}")
    fingerprint = (
        _sha256(config_fingerprint, "config_fingerprint")
        if config_fingerprint is not None
        else None
    )
    selected = [
        record
        for record in records
        if record.task == task
        and (fingerprint is None or record.config_fingerprint == fingerprint)
    ]
    if not selected:
        raise ConfidenceCalibrationError(f"no confidence records for {task}")
    rubric_versions = {record.rubric_version for record in selected}
    if len(rubric_versions) != 1:
        raise ConfidenceCalibrationError(
            f"{task} confidence records mix rubric versions"
        )
    return selected


def reliability_report(
    records: tuple[ConfidenceRecord, ...],
    *,
    task: Task,
    bin_count: int = 10,
    config_fingerprint: Optional[str] = None,
) -> ReliabilityReport:
    if bin_count < 2:
        raise ConfidenceCalibrationError("bin_count must be at least two")
    fingerprint = (
        _sha256(config_fingerprint, "config_fingerprint")
        if config_fingerprint is not None
        else None
    )
    selected = _task_records(records, task, fingerprint)
    grouped: list[list[ConfidenceRecord]] = [[] for _ in range(bin_count)]
    for record in selected:
        index = min(bin_count - 1, int(record.raw_confidence * bin_count))
        grouped[index].append(record)
    bins: list[ReliabilityBin] = []
    for index, values in enumerate(grouped):
        if not values:
            continue
        mean_confidence = sum(item.raw_confidence for item in values) / len(values)
        accuracy = sum(item.outcome for item in values) / len(values)
        bins.append(
            ReliabilityBin(
                lower_bound=index / bin_count,
                upper_bound=(index + 1) / bin_count,
                count=len(values),
                mean_confidence=mean_confidence,
                accuracy=accuracy,
                calibration_gap=abs(mean_confidence - accuracy),
            )
        )
    count = len(selected)
    epsilon = 1e-12
    brier = (
        sum((record.raw_confidence - float(record.outcome)) ** 2 for record in selected)
        / count
    )
    log_loss = (
        -sum(
            float(record.outcome) * math.log(max(epsilon, record.raw_confidence))
            + (1 - float(record.outcome))
            * math.log(max(epsilon, 1 - record.raw_confidence))
            for record in selected
        )
        / count
    )
    ece = sum(item.calibration_gap * item.count for item in bins) / count
    return ReliabilityReport(
        task=task,
        rubric_version=selected[0].rubric_version,
        config_fingerprint=fingerprint,
        samples=count,
        bins=tuple(bins),
        brier_score=brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        maximum_calibration_error=max(item.calibration_gap for item in bins),
        mean_confidence=sum(item.raw_confidence for item in selected) / count,
        outcome_rate=sum(item.outcome for item in selected) / count,
    )


def _wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, center - spread)


def _cost(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfidenceCalibrationError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfidenceCalibrationError(f"{field} must be a positive finite cost")
    return parsed


def _expect_cost(value: Any, expected: float, field: str) -> None:
    """Compare a derived cost, which — unlike a declared one — may be zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfidenceCalibrationError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ConfidenceCalibrationError(f"{field} must be a non-negative cost")
    if not math.isclose(parsed, expected, rel_tol=0.0, abs_tol=1e-12):
        raise ConfidenceCalibrationError(f"{field} does not match the evidence")


def threshold_curve(
    bins: tuple[CalibrationBin, ...],
    *,
    cost_model: CostModel,
    minimum_threshold_support: int,
    required_wilson_lower: float,
) -> tuple[OperatingPoint, ...]:
    """Score every candidate threshold the bins can express.

    Everything here is a function of the bins and the declared costs, so the
    loader recomputes it and rejects a curve that was edited after the fact.
    """
    total = sum(item.count for item in bins)
    points: list[OperatingPoint] = []
    for candidate in sorted({item.calibrated_probability for item in bins}):
        eligible = [item for item in bins if item.calibrated_probability >= candidate]
        autonomous = sum(item.count for item in eligible)
        true_autonomy = sum(item.successes for item in eligible)
        false_autonomy = autonomous - true_autonomy
        abstained = total - autonomous
        expected_cost = (
            false_autonomy * cost_model.false_autonomy_cost
            + abstained * cost_model.abstention_cost
        ) / total
        points.append(
            OperatingPoint(
                threshold=candidate,
                autonomous=autonomous,
                abstained=abstained,
                true_autonomy=true_autonomy,
                false_autonomy=false_autonomy,
                autonomous_success_rate=true_autonomy / autonomous,
                wilson_lower=_wilson_lower(true_autonomy, autonomous),
                coverage=autonomous / total,
                expected_cost_per_action=expected_cost,
                meets_support_floor=autonomous >= minimum_threshold_support,
                meets_wilson_floor=(
                    _wilson_lower(true_autonomy, autonomous) >= required_wilson_lower
                ),
            )
        )
    return tuple(points)


def _select_operating_point(
    curve: tuple[OperatingPoint, ...],
) -> Optional[OperatingPoint]:
    """Cheapest eligible point; a tie goes to the more conservative threshold."""
    eligible = [point for point in curve if point.eligible]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda point: (point.expected_cost_per_action, -point.threshold),
    )


def _artifact_payload(
    *,
    artifact_version: str,
    task: Task,
    rubric_version: str,
    source_sha256: str,
    config_fingerprint: str,
    sample_count: int,
    bins: tuple[CalibrationBin, ...],
    autonomy_threshold: Optional[float],
    threshold_support: int,
    threshold_wilson_lower: Optional[float],
    required_wilson_lower: float,
    minimum_threshold_support: int,
    cost_model: CostModel,
    curve: tuple[OperatingPoint, ...],
    always_abstain_cost: float,
    always_autonomous_cost: float,
    selected_cost: Optional[float],
    evidence_sources: tuple[str, ...],
    autonomy_blocked_reason: Optional[str],
    built_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_version": artifact_version,
        "task": task,
        "rubric_version": rubric_version,
        "config_fingerprint": config_fingerprint,
        "source_sha256": source_sha256,
        "sample_count": sample_count,
        "bins": [asdict(item) for item in bins],
        "autonomy_threshold": autonomy_threshold,
        "threshold_support": threshold_support,
        "threshold_wilson_lower": threshold_wilson_lower,
        "required_wilson_lower": required_wilson_lower,
        "minimum_threshold_support": minimum_threshold_support,
        "cost_model": asdict(cost_model),
        "threshold_curve": [asdict(item) for item in curve],
        "always_abstain_cost": always_abstain_cost,
        "always_autonomous_cost": always_autonomous_cost,
        "selected_cost": selected_cost,
        "evidence_sources": list(evidence_sources),
        "autonomy_blocked_reason": autonomy_blocked_reason,
        "selection_rule": SELECTION_RULE,
        "built_at": built_at.isoformat(),
    }


def _monotonic_bin_probabilities(
    chunks: list[list[ConfidenceRecord]],
) -> list[float]:
    """Pool adjacent violators so calibrated probability never decreases."""
    blocks: list[dict[str, Any]] = []
    for index, values in enumerate(chunks):
        blocks.append(
            {
                "indices": [index],
                "count": len(values),
                "successes": sum(item.outcome for item in values),
            }
        )
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_probability = (left["successes"] + 1) / (left["count"] + 2)
            right_probability = (right["successes"] + 1) / (right["count"] + 2)
            if left_probability <= right_probability:
                break
            blocks[-2:] = [
                {
                    "indices": left["indices"] + right["indices"],
                    "count": left["count"] + right["count"],
                    "successes": left["successes"] + right["successes"],
                }
            ]
    probabilities = [0.0] * len(chunks)
    for block in blocks:
        probability = (block["successes"] + 1) / (block["count"] + 2)
        for index in block["indices"]:
            probabilities[index] = probability
    return probabilities


def _equal_frequency_chunks(
    selected: list[ConfidenceRecord],
    *,
    maximum_bins: int,
    minimum_bin_samples: int,
) -> list[list[ConfidenceRecord]]:
    """Split without placing identical confidence values in different bins."""
    maximum = min(maximum_bins, len(selected) // minimum_bin_samples)
    for requested in range(maximum, 1, -1):
        cuts = [0]
        for index in range(1, requested):
            cut = index * len(selected) // requested
            while (
                cut < len(selected)
                and selected[cut - 1].raw_confidence == selected[cut].raw_confidence
            ):
                cut += 1
            if cut < len(selected) and cut > cuts[-1]:
                cuts.append(cut)
        cuts.append(len(selected))
        chunks = [
            selected[start:end] for start, end in zip(cuts, cuts[1:]) if end > start
        ]
        if len(chunks) >= 2 and all(
            len(values) >= minimum_bin_samples for values in chunks
        ):
            return chunks
    raise ConfidenceCalibrationError(
        "calibration requires at least two supported confidence bins"
    )


def build_calibration_artifact(
    records: tuple[ConfidenceRecord, ...],
    *,
    task: Task,
    source_sha256: str,
    config_fingerprint: str,
    artifact_version: str,
    minimum_samples: int = 100,
    minimum_bin_samples: int = 20,
    maximum_bins: int = 10,
    minimum_threshold_support: int = 40,
    required_wilson_lower: float = 0.90,
    false_autonomy_cost: float = 20.0,
    abstention_cost: float = 1.0,
) -> CalibrationArtifact:
    fingerprint = _sha256(config_fingerprint, "config_fingerprint")
    selected = sorted(
        _task_records(records, task, fingerprint),
        key=lambda item: item.raw_confidence,
    )
    if len(selected) < minimum_samples:
        raise ConfidenceCalibrationError(
            f"{task} has {len(selected)} samples; requires {minimum_samples}"
        )
    if minimum_bin_samples < 2:
        raise ConfidenceCalibrationError("minimum_bin_samples must be at least two")
    if not 0 < required_wilson_lower < 1:
        raise ConfidenceCalibrationError(
            "required_wilson_lower must be between zero and one"
        )
    if maximum_bins < 2:
        raise ConfidenceCalibrationError("maximum_bins must be at least two")
    if minimum_threshold_support < 1:
        raise ConfidenceCalibrationError("minimum_threshold_support must be positive")
    chunks = _equal_frequency_chunks(
        selected,
        maximum_bins=maximum_bins,
        minimum_bin_samples=minimum_bin_samples,
    )
    calibrated_probabilities = _monotonic_bin_probabilities(chunks)
    bins = tuple(
        CalibrationBin(
            upper_bound=(
                1.0 if index == len(chunks) - 1 else values[-1].raw_confidence
            ),
            count=len(values),
            successes=sum(item.outcome for item in values),
            calibrated_probability=calibrated_probabilities[index],
        )
        for index, values in enumerate(chunks)
    )

    cost_model = CostModel(
        false_autonomy_cost=_cost(false_autonomy_cost, "false_autonomy_cost"),
        abstention_cost=_cost(abstention_cost, "abstention_cost"),
    )
    curve = threshold_curve(
        bins,
        cost_model=cost_model,
        minimum_threshold_support=minimum_threshold_support,
        required_wilson_lower=required_wilson_lower,
    )
    point = _select_operating_point(curve)

    # Evidence provenance is the last gate: a corpus that was replayed or
    # generated still earns a curve and a reliability picture, but it must not
    # be able to hand the runtime a threshold.
    evidence_sources = tuple(sorted({record.evidence_source for record in selected}))
    blocked_reason: Optional[str] = None
    if evidence_sources != (_AUTONOMY_EVIDENCE,):
        blocked_reason = (
            "autonomy requires evidence observed only from live benchmark runs; "
            f"this corpus contains {', '.join(evidence_sources)}"
        )
    elif point is None:
        blocked_reason = (
            "no operating point reached "
            f"{minimum_threshold_support} autonomous observations with a Wilson "
            f"lower bound of {required_wilson_lower}"
        )

    threshold = None if blocked_reason else point.threshold  # type: ignore[union-attr]
    threshold_support = 0 if blocked_reason else point.autonomous  # type: ignore[union-attr]
    threshold_lower = None if blocked_reason else point.wilson_lower  # type: ignore[union-attr]
    selected_cost = (
        None if blocked_reason else point.expected_cost_per_action  # type: ignore[union-attr]
    )
    failures = sum(item.count - item.successes for item in bins)

    built_at = datetime.now(timezone.utc)
    payload = _artifact_payload(
        artifact_version=_string(artifact_version, "artifact_version"),
        task=task,
        rubric_version=selected[0].rubric_version,
        config_fingerprint=fingerprint,
        source_sha256=_sha256(source_sha256, "source_sha256"),
        sample_count=len(selected),
        bins=bins,
        autonomy_threshold=threshold,
        threshold_support=threshold_support,
        threshold_wilson_lower=threshold_lower,
        required_wilson_lower=required_wilson_lower,
        minimum_threshold_support=minimum_threshold_support,
        cost_model=cost_model,
        curve=curve,
        always_abstain_cost=cost_model.abstention_cost,
        always_autonomous_cost=(
            failures * cost_model.false_autonomy_cost / len(selected)
        ),
        selected_cost=selected_cost,
        evidence_sources=evidence_sources,
        autonomy_blocked_reason=blocked_reason,
        built_at=built_at,
    )
    artifact_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CalibrationArtifact(
        artifact_version=payload["artifact_version"],
        task=task,
        rubric_version=selected[0].rubric_version,
        config_fingerprint=fingerprint,
        source_sha256=payload["source_sha256"],
        sample_count=len(selected),
        bins=bins,
        autonomy_threshold=threshold,
        threshold_support=threshold_support,
        threshold_wilson_lower=threshold_lower,
        required_wilson_lower=required_wilson_lower,
        minimum_threshold_support=minimum_threshold_support,
        cost_model=cost_model,
        threshold_curve=curve,
        always_abstain_cost=payload["always_abstain_cost"],
        always_autonomous_cost=payload["always_autonomous_cost"],
        selected_cost=selected_cost,
        evidence_sources=evidence_sources,
        autonomy_blocked_reason=blocked_reason,
        built_at=built_at,
        artifact_sha256=artifact_sha,
    )


def save_calibration_artifact(path: Path, artifact: CalibrationArtifact) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_calibration_artifact(path: Path) -> CalibrationArtifact:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfidenceCalibrationError(
            f"calibration artifact does not exist: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfidenceCalibrationError(
            f"calibration artifact is invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfidenceCalibrationError("calibration artifact must be an object")
    expected = {
        "schema_version",
        "artifact_version",
        "task",
        "rubric_version",
        "config_fingerprint",
        "source_sha256",
        "sample_count",
        "bins",
        "autonomy_threshold",
        "threshold_support",
        "threshold_wilson_lower",
        "required_wilson_lower",
        "minimum_threshold_support",
        "cost_model",
        "threshold_curve",
        "always_abstain_cost",
        "always_autonomous_cost",
        "selected_cost",
        "evidence_sources",
        "autonomy_blocked_reason",
        "selection_rule",
        "built_at",
        "artifact_sha256",
    }
    if set(payload) != expected or payload["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise ConfidenceCalibrationError("calibration artifact keys do not match v2")
    task = _string(payload["task"], "task")
    if task not in _TASKS:
        raise ConfidenceCalibrationError(f"unsupported confidence task: {task}")
    bins_payload = payload["bins"]
    if not isinstance(bins_payload, list) or not bins_payload:
        raise ConfidenceCalibrationError("calibration artifact bins are missing")
    bins: list[CalibrationBin] = []
    previous_upper = 0.0
    previous_probability = 0.0
    for index, value in enumerate(bins_payload):
        if not isinstance(value, dict) or set(value) != {
            "upper_bound",
            "count",
            "successes",
            "calibrated_probability",
        }:
            raise ConfidenceCalibrationError(f"calibration bin {index} is malformed")
        upper = _probability(value["upper_bound"], f"bin {index}.upper_bound")
        probability = _probability(
            value["calibrated_probability"],
            f"bin {index}.calibrated_probability",
        )
        count = value["count"]
        successes = value["successes"]
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or not isinstance(successes, int)
            or isinstance(successes, bool)
            or not 0 <= successes <= count
            or (index > 0 and upper <= previous_upper)
            or probability < previous_probability
        ):
            raise ConfidenceCalibrationError(f"calibration bin {index} is invalid")
        previous_upper = upper
        previous_probability = probability
        bins.append(CalibrationBin(upper, count, successes, probability))
    if bins[-1].upper_bound != 1.0:
        raise ConfidenceCalibrationError("last calibration bin must end at 1.0")
    built_at = _timestamp(payload["built_at"], "built_at")
    rubric_version = _string(payload["rubric_version"], "rubric_version")
    config_fingerprint = _sha256(payload["config_fingerprint"], "config_fingerprint")
    source_sha = _sha256(payload["source_sha256"], "source_sha256")
    artifact_version = _string(payload["artifact_version"], "artifact_version")
    threshold = payload["autonomy_threshold"]
    parsed_threshold = (
        None if threshold is None else _probability(threshold, "autonomy_threshold")
    )
    threshold_lower = payload["threshold_wilson_lower"]
    parsed_lower = (
        None
        if threshold_lower is None
        else _probability(threshold_lower, "threshold_wilson_lower")
    )
    sample_count = payload["sample_count"]
    threshold_support = payload["threshold_support"]
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 1
        or sample_count != sum(item.count for item in bins)
    ):
        raise ConfidenceCalibrationError(
            "sample_count must equal the calibration bin population"
        )
    if (
        not isinstance(threshold_support, int)
        or isinstance(threshold_support, bool)
        or not 0 <= threshold_support <= sample_count
    ):
        raise ConfidenceCalibrationError("threshold_support is invalid")
    required_lower = _probability(
        payload["required_wilson_lower"], "required_wilson_lower"
    )
    if parsed_threshold is None:
        if threshold_support != 0 or parsed_lower is not None:
            raise ConfidenceCalibrationError(
                "artifact without a threshold cannot declare threshold evidence"
            )
    elif threshold_support < 1 or parsed_lower is None or parsed_lower < required_lower:
        raise ConfidenceCalibrationError(
            "autonomy threshold lacks its required Wilson evidence"
        )
    else:
        eligible = [
            item for item in bins if item.calibrated_probability >= parsed_threshold
        ]
        expected_support = sum(item.count for item in eligible)
        expected_lower = _wilson_lower(
            sum(item.successes for item in eligible),
            expected_support,
        )
        if (
            parsed_threshold not in {item.calibrated_probability for item in bins}
            or threshold_support != expected_support
            or not math.isclose(
                parsed_lower, expected_lower, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            raise ConfidenceCalibrationError(
                "autonomy threshold evidence does not match its bins"
            )

    cost_payload = payload["cost_model"]
    if not isinstance(cost_payload, dict) or set(cost_payload) != {
        "false_autonomy_cost",
        "abstention_cost",
    }:
        raise ConfidenceCalibrationError("cost model is malformed")
    cost_model = CostModel(
        false_autonomy_cost=_cost(
            cost_payload["false_autonomy_cost"], "false_autonomy_cost"
        ),
        abstention_cost=_cost(cost_payload["abstention_cost"], "abstention_cost"),
    )
    minimum_support = payload["minimum_threshold_support"]
    if (
        not isinstance(minimum_support, int)
        or isinstance(minimum_support, bool)
        or minimum_support < 1
    ):
        raise ConfidenceCalibrationError("minimum_threshold_support is invalid")
    if payload["selection_rule"] != SELECTION_RULE:
        raise ConfidenceCalibrationError(
            "calibration artifact was built under a different selection rule"
        )
    sources = payload["evidence_sources"]
    if (
        not isinstance(sources, list)
        or not sources
        or any(item not in _EVIDENCE_SOURCES for item in sources)
        or list(sources) != sorted(set(sources))
    ):
        raise ConfidenceCalibrationError("evidence_sources is invalid")
    blocked_reason = payload["autonomy_blocked_reason"]
    if blocked_reason is not None and not isinstance(blocked_reason, str):
        raise ConfidenceCalibrationError("autonomy_blocked_reason must be a string")

    # The curve is a pure function of the bins and the declared costs, so a
    # hand-edited operating point cannot survive recomputation.
    curve = threshold_curve(
        tuple(bins),
        cost_model=cost_model,
        minimum_threshold_support=minimum_support,
        required_wilson_lower=required_lower,
    )
    if payload["threshold_curve"] != [asdict(item) for item in curve]:
        raise ConfidenceCalibrationError(
            "threshold curve does not match the calibration bins and cost model"
        )
    selected_point = _select_operating_point(curve)
    failures = sum(item.count - item.successes for item in bins)
    _expect_cost(
        payload["always_abstain_cost"],
        cost_model.abstention_cost,
        "always_abstain_cost",
    )
    _expect_cost(
        payload["always_autonomous_cost"],
        failures * cost_model.false_autonomy_cost / sample_count,
        "always_autonomous_cost",
    )

    if parsed_threshold is None:
        if payload["selected_cost"] is not None:
            raise ConfidenceCalibrationError(
                "artifact without a threshold cannot declare a selected cost"
            )
        if not blocked_reason:
            raise ConfidenceCalibrationError(
                "an artifact that grants no autonomy must say why"
            )
    else:
        if blocked_reason is not None:
            raise ConfidenceCalibrationError(
                "a blocked artifact cannot also carry an autonomy threshold"
            )
        if list(sources) != [_AUTONOMY_EVIDENCE]:
            raise ConfidenceCalibrationError(
                "autonomy requires evidence observed only from live benchmark runs"
            )
        if selected_point is None or not math.isclose(
            selected_point.threshold, parsed_threshold, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ConfidenceCalibrationError(
                "autonomy threshold is not the operating point the rule selects"
            )
        _expect_cost(
            payload["selected_cost"],
            selected_point.expected_cost_per_action,
            "selected_cost",
        )

    canonical = {
        key: value for key, value in payload.items() if key != "artifact_sha256"
    }
    expected_sha = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if payload["artifact_sha256"] != expected_sha:
        raise ConfidenceCalibrationError("calibration artifact digest mismatch")
    return CalibrationArtifact(
        artifact_version=artifact_version,
        task=task,
        rubric_version=rubric_version,
        config_fingerprint=config_fingerprint,
        source_sha256=source_sha,
        sample_count=sample_count,
        bins=tuple(bins),
        autonomy_threshold=parsed_threshold,
        threshold_support=threshold_support,
        threshold_wilson_lower=parsed_lower,
        required_wilson_lower=required_lower,
        minimum_threshold_support=minimum_support,
        cost_model=cost_model,
        threshold_curve=curve,
        always_abstain_cost=float(payload["always_abstain_cost"]),
        always_autonomous_cost=float(payload["always_autonomous_cost"]),
        selected_cost=(
            None
            if payload["selected_cost"] is None
            else float(payload["selected_cost"])
        ),
        evidence_sources=tuple(sources),
        autonomy_blocked_reason=blocked_reason,
        built_at=built_at,
        artifact_sha256=expected_sha,
    )


def calibrate_confidence(
    raw_confidence: float,
    artifact: CalibrationArtifact,
    *,
    task: Task,
    config_fingerprint: str,
) -> CalibratedConfidence:
    raw = _probability(raw_confidence, "raw_confidence")
    if artifact.task != task:
        raise ConfidenceCalibrationError(
            f"artifact task {artifact.task} cannot calibrate {task}"
        )
    fingerprint = _sha256(config_fingerprint, "config_fingerprint")
    if artifact.config_fingerprint != fingerprint:
        raise ConfidenceCalibrationError(
            "calibration artifact configuration does not match runtime"
        )
    selected = artifact.bins[-1]
    for item in artifact.bins:
        if raw <= item.upper_bound:
            selected = item
            break
    return CalibratedConfidence(
        task=task,
        raw_confidence=raw,
        calibrated_probability=selected.calibrated_probability,
        artifact_version=artifact.artifact_version,
        artifact_sha256=artifact.artifact_sha256,
        autonomy_threshold=artifact.autonomy_threshold,
        autonomy_blocked_reason=artifact.autonomy_blocked_reason,
    )


def calibration_drift(
    reference: ReliabilityReport,
    current: ReliabilityReport,
    *,
    maximum_ece_increase: float = 0.05,
    maximum_brier_increase: float = 0.05,
    maximum_confidence_shift: float = 0.10,
) -> dict[str, Any]:
    if reference.task != current.task:
        raise ConfidenceCalibrationError("drift reports must use the same task")
    if reference.rubric_version != current.rubric_version:
        raise ConfidenceCalibrationError(
            "drift reports must use the same rubric version"
        )
    if reference.config_fingerprint != current.config_fingerprint:
        raise ConfidenceCalibrationError(
            "drift reports must use the same configuration fingerprint"
        )
    deltas = {
        "expected_calibration_error": (
            current.expected_calibration_error - reference.expected_calibration_error
        ),
        "brier_score": current.brier_score - reference.brier_score,
        "mean_confidence": current.mean_confidence - reference.mean_confidence,
        "outcome_rate": current.outcome_rate - reference.outcome_rate,
    }
    reasons: list[str] = []
    if deltas["expected_calibration_error"] > maximum_ece_increase:
        reasons.append("expected calibration error increased")
    if deltas["brier_score"] > maximum_brier_increase:
        reasons.append("Brier score increased")
    if abs(deltas["mean_confidence"]) > maximum_confidence_shift:
        reasons.append("mean confidence shifted")
    return {
        "task": reference.task,
        "reference_samples": reference.samples,
        "current_samples": current.samples,
        "deltas": deltas,
        "status": "DRIFTED" if reasons else "STABLE",
        "reasons": reasons,
    }
