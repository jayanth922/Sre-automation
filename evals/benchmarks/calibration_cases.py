#!/usr/bin/env python3
"""Build a blinded semantic-grader review set from raw benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from benchmarks.structured_grading import (
    EXPECTED_RUBRIC_VERSION,
    extract_structured_output,
    load_rubric,
)

SCHEMA_VERSION = 1
EXPECTED_RUBRIC_SHA256 = load_rubric().sha256
_RECORD_KEYS = {
    "schema_version",
    "recorded_at",
    "scenario",
    "dataset_version",
    "scenario_version",
    "oracle_status",
    "application_status",
    "raw_output_sha256",
    "raw_output",
    "score",
}
# Additive producer changes must not brick the consumer. `harness_approvals`
# was added to `append_grader_record` in 2b73497 and never added here, and
# because the check was an exact set equality, every grader record written
# since -- both paid campaigns, all of it -- was rejected as schema-invalid.
# The calibration corpus could never have been built from real evidence, which
# is a strange way for "calibration needs ~100 more trials" to be false.
#
# Unknown keys are still refused. Known-additive ones are named here instead,
# so the next field added to the producer degrades to "not carried" rather
# than "nothing loads at all".
_OPTIONAL_RECORD_KEYS = {
    "harness_approvals",
    "expected_evidence_coverage",
}


class CalibrationCaseError(ValueError):
    """Raw grader evidence cannot produce a trustworthy blinded case set."""


@dataclass(frozen=True)
class CalibrationCaseSet:
    review_cases: tuple[dict[str, Any], ...]
    private_mapping: tuple[dict[str, Any], ...]
    input_sha256: str
    key_fingerprint: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationCaseError(f"{field} must be a non-empty string")
    return value.strip()


def _load_key(path: Path) -> bytes:
    try:
        key = path.read_bytes().strip()
    except FileNotFoundError as exc:
        raise CalibrationCaseError(f"blind key does not exist: {path}") from exc
    if len(key) < 32:
        raise CalibrationCaseError("blind key must contain at least 32 bytes")
    return key


def _parse_records(raw: bytes) -> list[dict[str, Any]]:
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise CalibrationCaseError("grader evidence is empty")
    records: list[dict[str, Any]] = []
    seen_outputs: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise CalibrationCaseError(f"line {line_number} is empty")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationCaseError(f"line {line_number} is invalid JSON") from exc
        if not isinstance(record, dict):
            raise CalibrationCaseError(f"line {line_number} is not an object")
        keys = set(record)
        unknown = keys - _RECORD_KEYS - _OPTIONAL_RECORD_KEYS
        if not _RECORD_KEYS <= keys or unknown:
            raise CalibrationCaseError(
                f"line {line_number} keys do not match grader-record schema v1"
            )
        if record["schema_version"] != 1:
            raise CalibrationCaseError(
                f"line {line_number} has unsupported grader-record schema"
            )
        output_sha = _string(
            record["raw_output_sha256"], f"line {line_number}.raw_output_sha256"
        )
        if len(output_sha) != 64 or any(
            character not in "0123456789abcdef" for character in output_sha
        ):
            raise CalibrationCaseError(
                f"line {line_number}.raw_output_sha256 must be lowercase SHA-256"
            )
        if output_sha in seen_outputs:
            raise CalibrationCaseError("duplicate raw output cannot be labeled twice")
        seen_outputs.add(output_sha)
        raw_output = record["raw_output"]
        if not isinstance(raw_output, dict) or set(raw_output) != {
            "summary_text",
            "events",
        }:
            raise CalibrationCaseError(
                f"line {line_number}.raw_output does not match schema"
            )
        events = raw_output["events"]
        if not isinstance(events, list):
            raise CalibrationCaseError(f"line {line_number}.events must be a list")
        encoded_output = json.dumps(
            raw_output, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if not hmac.compare_digest(output_sha, _sha256(encoded_output)):
            raise CalibrationCaseError(
                f"line {line_number}.raw_output_sha256 does not match raw_output"
            )
        structured = extract_structured_output(events)
        if not isinstance(structured, dict):
            raise CalibrationCaseError(
                f"line {line_number} has no structured benchmark evaluation"
            )
        score = record["score"]
        grade = score.get("structured_grade") if isinstance(score, dict) else None
        if (
            not isinstance(grade, dict)
            or grade.get("rubric_version") != EXPECTED_RUBRIC_VERSION
            or grade.get("rubric_sha256") != EXPECTED_RUBRIC_SHA256
        ):
            raise CalibrationCaseError(
                f"line {line_number} is not pinned to the current "
                f"{EXPECTED_RUBRIC_VERSION} rubric digest"
            )
        records.append(record)
    return records


def build_case_set(
    raw: bytes,
    *,
    blind_key: bytes,
    limit: Optional[int] = None,
) -> CalibrationCaseSet:
    if len(blind_key) < 32:
        raise CalibrationCaseError("blind key must contain at least 32 bytes")
    if limit is not None and limit < 1:
        raise CalibrationCaseError("limit must be positive")
    records = _parse_records(raw)

    def keyed_digest(record: dict[str, Any]) -> str:
        material = record["raw_output_sha256"].encode("utf-8")
        return hmac.new(blind_key, material, hashlib.sha256).hexdigest()

    selected = sorted(records, key=keyed_digest)
    if limit is not None:
        selected = selected[:limit]

    review_cases = []
    mapping = []
    for record in selected:
        blind_case_id = f"case-{keyed_digest(record)[:24]}"
        raw_output = record["raw_output"]
        structured = extract_structured_output(raw_output["events"])
        review_cases.append(
            {
                "schema_version": SCHEMA_VERSION,
                "rubric_version": EXPECTED_RUBRIC_VERSION,
                "blind_case_id": blind_case_id,
                "review_input": {
                    "summary_text": raw_output["summary_text"],
                    "benchmark_evaluation": structured,
                    "timeline_events": raw_output["events"],
                },
            }
        )
        mapping.append(
            {
                "blind_case_id": blind_case_id,
                "scenario": record["scenario"],
                "dataset_version": record["dataset_version"],
                "scenario_version": record["scenario_version"],
                "oracle_status": record["oracle_status"],
                "application_status": record["application_status"],
                "source_output_sha256": record["raw_output_sha256"],
                # Stays in the private mapping, never the review case: a judge
                # told the harness approved the action is no longer blinded to
                # how the run reached its outcome.
                "harness_approvals": record.get("harness_approvals"),
            }
        )
    return CalibrationCaseSet(
        review_cases=tuple(review_cases),
        private_mapping=tuple(mapping),
        input_sha256=_sha256(raw),
        key_fingerprint=_sha256(blind_key),
    )


def _jsonl(rows: Sequence[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for row in rows
    )


def write_case_set(
    case_set: CalibrationCaseSet,
    *,
    review_path: Path,
    mapping_path: Path,
    manifest_path: Path,
) -> None:
    review = _jsonl(case_set.review_cases)
    mapping = _jsonl(case_set.private_mapping)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "rubric_version": EXPECTED_RUBRIC_VERSION,
        "cases": len(case_set.review_cases),
        "input_sha256": case_set.input_sha256,
        "blind_key_fingerprint": case_set.key_fingerprint,
        "review_sha256": _sha256(review),
        "private_mapping_sha256": _sha256(mapping),
    }
    for path in (review_path, mapping_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_bytes(review)
    mapping_path.write_bytes(mapping)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("grader_records", type=Path)
    parser.add_argument("--blind-key-file", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--private-mapping-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        raw = args.grader_records.read_bytes()
        case_set = build_case_set(
            raw,
            blind_key=_load_key(args.blind_key_file),
            limit=args.limit,
        )
        write_case_set(
            case_set,
            review_path=args.review_output,
            mapping_path=args.private_mapping_output,
            manifest_path=args.manifest_output,
        )
    except (OSError, CalibrationCaseError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
