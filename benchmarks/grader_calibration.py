#!/usr/bin/env python3
"""Blinded human-label loading and inter-rater agreement for A04 graders."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

RUBRIC_VERSION = "sre-structured-v1"
CRITERIA = ("causal_chain", "evidence_support")
HumanJudgment = Literal["PASS", "FAIL"]


class CalibrationError(ValueError):
    """Calibration evidence is missing, malformed, or not release-ready."""


@dataclass(frozen=True)
class HumanLabel:
    blind_case_id: str
    labeler_id: str
    labeled_at: datetime
    criterion_labels: dict[str, HumanJudgment]
    rationales: dict[str, str]


@dataclass(frozen=True)
class Agreement:
    criterion: str
    cases: int
    pairwise_comparisons: int
    percent_agreement: float
    cohen_kappa: Optional[float]


@dataclass(frozen=True)
class AgreementReport:
    rubric_version: str
    cases: int
    labelers: int
    criteria: dict[str, Agreement]

    def to_dict(self) -> dict:
        return {
            "rubric_version": self.rubric_version,
            "cases": self.cases,
            "labelers": self.labelers,
            "criteria": {
                name: {
                    "criterion": value.criterion,
                    "cases": value.cases,
                    "pairwise_comparisons": value.pairwise_comparisons,
                    "percent_agreement": value.percent_agreement,
                    "cohen_kappa": value.cohen_kappa,
                }
                for name, value in self.criteria.items()
            },
        }


def _string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value, field: str) -> datetime:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalibrationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CalibrationError(f"{field} must be timezone-aware")
    return parsed


def _parse_label(payload: object, line_number: int) -> HumanLabel:
    field = f"line {line_number}"
    if not isinstance(payload, dict):
        raise CalibrationError(f"{field} must be an object")
    expected = {
        "schema_version",
        "rubric_version",
        "blind_case_id",
        "labeler_id",
        "labeled_at",
        "criterion_labels",
        "rationales",
    }
    if set(payload) != expected:
        raise CalibrationError(f"{field} keys do not match calibration schema")
    if payload["schema_version"] != 1:
        raise CalibrationError(f"{field} has unsupported schema version")
    if payload["rubric_version"] != RUBRIC_VERSION:
        raise CalibrationError(f"{field} has the wrong rubric version")

    labels = payload["criterion_labels"]
    rationales = payload["rationales"]
    if not isinstance(labels, dict) or set(labels) != set(CRITERIA):
        raise CalibrationError(f"{field}.criterion_labels are incomplete")
    if not isinstance(rationales, dict) or set(rationales) != set(CRITERIA):
        raise CalibrationError(f"{field}.rationales are incomplete")
    normalized_labels: dict[str, HumanJudgment] = {}
    normalized_rationales: dict[str, str] = {}
    for criterion in CRITERIA:
        judgment = labels[criterion]
        if judgment not in {"PASS", "FAIL"}:
            raise CalibrationError(f"{field}.{criterion} judgment must be PASS or FAIL")
        normalized_labels[criterion] = judgment
        normalized_rationales[criterion] = _string(
            rationales[criterion], f"{field}.{criterion} rationale"
        )
    return HumanLabel(
        blind_case_id=_string(payload["blind_case_id"], f"{field}.blind_case_id"),
        labeler_id=_string(payload["labeler_id"], f"{field}.labeler_id"),
        labeled_at=_timestamp(payload["labeled_at"], f"{field}.labeled_at"),
        criterion_labels=normalized_labels,
        rationales=normalized_rationales,
    )


def load_human_labels(path: Path) -> tuple[HumanLabel, ...]:
    """Load JSONL labels without exposing scenario IDs or ground truth."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CalibrationError(
            f"calibration label file does not exist: {path}"
        ) from exc
    if not lines:
        raise CalibrationError("calibration label file is empty")
    labels: list[HumanLabel] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise CalibrationError(f"line {line_number} is empty")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"line {line_number} is invalid JSON") from exc
        label = _parse_label(payload, line_number)
        identity = (label.blind_case_id, label.labeler_id)
        if identity in seen:
            raise CalibrationError("a labeler may label each blinded case only once")
        seen.add(identity)
        labels.append(label)
    return tuple(labels)


def _criterion_agreement(
    criterion: str, by_case: dict[str, list[HumanLabel]]
) -> Agreement:
    pairs: list[tuple[HumanJudgment, HumanJudgment]] = []
    for labels in by_case.values():
        left, right = sorted(labels, key=lambda label: label.labeler_id)
        pairs.append(
            (
                left.criterion_labels[criterion],
                right.criterion_labels[criterion],
            )
        )
    if not pairs:
        raise CalibrationError(f"{criterion} has no independent labeler comparisons")
    observed = sum(1 for left, right in pairs if left == right) / len(pairs)
    left_pass = sum(1 for left, _ in pairs if left == "PASS") / len(pairs)
    right_pass = sum(1 for _, right in pairs if right == "PASS") / len(pairs)
    expected = left_pass * right_pass + (1 - left_pass) * (1 - right_pass)
    kappa = None if expected == 1.0 else (observed - expected) / (1 - expected)
    return Agreement(
        criterion=criterion,
        cases=len(by_case),
        pairwise_comparisons=len(pairs),
        percent_agreement=observed,
        cohen_kappa=kappa,
    )


def measure_agreement(labels: tuple[HumanLabel, ...]) -> AgreementReport:
    if not labels:
        raise CalibrationError("no human labels were provided")
    by_case: dict[str, list[HumanLabel]] = {}
    labelers: set[str] = set()
    for label in labels:
        by_case.setdefault(label.blind_case_id, []).append(label)
        labelers.add(label.labeler_id)
    incorrectly_labeled = sorted(
        case_id for case_id, values in by_case.items() if len(values) != 2
    )
    if incorrectly_labeled:
        raise CalibrationError(
            "cases require exactly two independent labelers: " f"{incorrectly_labeled}"
        )
    labeler_pairs = {
        tuple(sorted(value.labeler_id for value in values))
        for values in by_case.values()
    }
    if len(labeler_pairs) != 1:
        raise CalibrationError(
            "the same two independent labelers must rate every blinded case"
        )
    criteria = {
        criterion: _criterion_agreement(criterion, by_case) for criterion in CRITERIA
    }
    return AgreementReport(
        rubric_version=RUBRIC_VERSION,
        cases=len(by_case),
        labelers=len(labelers),
        criteria=criteria,
    )


def require_release_ready(
    report: AgreementReport,
    *,
    minimum_cases: int,
    minimum_kappa: float,
) -> None:
    """Fail closed until every semantic criterion meets the release gate."""
    if report.cases < minimum_cases:
        raise CalibrationError(
            f"calibration has {report.cases} cases; requires {minimum_cases}"
        )
    for criterion, agreement in report.criteria.items():
        if agreement.cohen_kappa is None:
            raise CalibrationError(
                f"{criterion} kappa is undefined; labels lack class variation"
            )
        if agreement.cohen_kappa < minimum_kappa:
            raise CalibrationError(
                f"{criterion} kappa {agreement.cohen_kappa:.3f} "
                f"is below {minimum_kappa:.3f}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure blinded A04 human-label agreement"
    )
    parser.add_argument("labels", type=Path, help="Calibration labels JSONL")
    parser.add_argument("--minimum-cases", type=int, default=20)
    parser.add_argument("--minimum-kappa", type=float, default=0.6)
    args = parser.parse_args()
    try:
        report = measure_agreement(load_human_labels(args.labels))
        require_release_ready(
            report,
            minimum_cases=args.minimum_cases,
            minimum_kappa=args.minimum_kappa,
        )
    except CalibrationError as exc:
        parser.error(str(exc))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
