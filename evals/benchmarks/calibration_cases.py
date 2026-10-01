#!/usr/bin/env python3
"""Build a blinded semantic-grader review set from raw benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

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
class SkippedRecord:
    """A well-formed grader record that is not reviewable.

    Not the same thing as corrupt evidence. A record whose digest does not
    match its output, or whose keys are not the grader schema, says the file
    cannot be trusted and the whole build must stop. A record that carries no
    structured evaluation, or was graded against a rubric that has since
    changed, says only that this one run cannot be judged -- every other run in
    the file still can.
    """

    line_number: int
    scenario: Optional[str]
    reason: str
    # Which input file, in argument order, when several are combined.
    source_index: int = 0


@dataclass(frozen=True)
class ParsedEvidence:
    records: tuple[dict[str, Any], ...]
    skipped: tuple[SkippedRecord, ...]


@dataclass(frozen=True)
class CalibrationCaseSet:
    review_cases: tuple[dict[str, Any], ...]
    private_mapping: tuple[dict[str, Any], ...]
    input_sha256: str
    key_fingerprint: str
    # What was in the file but is not in the review set, and why. Dropping
    # cases silently would bias the corpus toward whatever the current rubric
    # happens to grade; reporting the drop keeps the bias measurable.
    skipped: tuple[SkippedRecord, ...] = ()
    # Reviewable records found, before `limit` narrows them.
    eligible_count: int = 0
    # One digest per input file, in argument order.
    input_sha256s: tuple[str, ...] = ()
    # None when no transcript store was given; see `attach_transcripts`.
    transcripts_attached: Optional[int] = None
    transcripts_missing: Optional[int] = None
    cases_without_transcripts: Optional[int] = None


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


def _parse_records(
    raw: bytes, earlier_outputs: frozenset[str] = frozenset()
) -> ParsedEvidence:
    """Split grader evidence into what can be reviewed and what cannot.

    Integrity failures still raise -- they mean the file is not the evidence it
    claims to be. Eligibility failures are collected instead: a corpus of 38
    records where 16 predate the current rubric should yield 22 cases, not an
    exception naming line 3.

    `earlier_outputs` are digests already taken from earlier input files. The
    same run copied into two report directories is one case, not a corrupt
    file, so it is skipped here; a repeat inside one file still raises.
    """
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise CalibrationCaseError("grader evidence is empty")
    records: list[dict[str, Any]] = []
    skipped: list[SkippedRecord] = []
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
        scenario = record.get("scenario")
        scenario = scenario if isinstance(scenario, str) else None
        structured = extract_structured_output(events)
        if not isinstance(structured, dict):
            skipped.append(
                SkippedRecord(
                    line_number=line_number,
                    scenario=scenario,
                    reason="no structured benchmark evaluation",
                )
            )
            continue
        score = record["score"]
        grade = score.get("structured_grade") if isinstance(score, dict) else None
        if (
            not isinstance(grade, dict)
            or grade.get("rubric_version") != EXPECTED_RUBRIC_VERSION
            or grade.get("rubric_sha256") != EXPECTED_RUBRIC_SHA256
        ):
            skipped.append(
                SkippedRecord(
                    line_number=line_number,
                    scenario=scenario,
                    reason=(
                        "is not pinned to the current "
                        f"{EXPECTED_RUBRIC_VERSION} rubric digest"
                    ),
                )
            )
            continue
        # Checked only once a record is reviewable. `_score_without_output`
        # records every run that produced nothing as `{"summary_text": "",
        # "events": []}`, so any two of them share a digest by construction.
        # Checking first made two harmless empty records abort the whole file:
        # four 2026-09-19 INVALID_SCENARIO rows kept every negative-control run
        # in reports/sre-bench-grades.jsonl out of the review set.
        if output_sha in earlier_outputs:
            skipped.append(
                SkippedRecord(
                    line_number=line_number,
                    scenario=scenario,
                    reason="already taken from an earlier input file",
                )
            )
            continue
        if output_sha in seen_outputs:
            raise CalibrationCaseError("duplicate raw output cannot be labeled twice")
        seen_outputs.add(output_sha)
        records.append(record)
    return ParsedEvidence(records=tuple(records), skipped=tuple(skipped))


def skip_reason_counts(skipped: Sequence[SkippedRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in skipped:
        counts[entry.reason] = counts.get(entry.reason, 0) + 1
    return counts


def _parse_sources(sources: Sequence[bytes]) -> ParsedEvidence:
    records: list[dict[str, Any]] = []
    skipped: list[SkippedRecord] = []
    taken: set[str] = set()
    for index, source in enumerate(sources):
        parsed = _parse_records(source, frozenset(taken))
        records.extend(parsed.records)
        taken.update(record["raw_output_sha256"] for record in parsed.records)
        skipped.extend(replace(entry, source_index=index) for entry in parsed.skipped)
    return ParsedEvidence(records=tuple(records), skipped=tuple(skipped))


def build_case_set(
    raw: Union[bytes, Sequence[bytes]],
    *,
    blind_key: bytes,
    limit: Optional[int] = None,
    transcripts: Optional[Mapping[str, bytes]] = None,
) -> CalibrationCaseSet:
    """Build the blinded set from one grader file or several.

    Run evidence is scattered across report directories -- 18 grades files on
    2026-10-01 -- so several files may be combined; a run copied into more
    than one is reviewed once. `transcripts` maps a specialist transcript's
    content digest to its canonical bytes (see `load_transcripts`); when given,
    each case carries the tool calls and returns its evidence cites.
    """
    if len(blind_key) < 32:
        raise CalibrationCaseError("blind key must contain at least 32 bytes")
    if limit is not None and limit < 1:
        raise CalibrationCaseError("limit must be positive")
    sources = [raw] if isinstance(raw, (bytes, bytearray)) else list(raw)
    if not sources:
        raise CalibrationCaseError("no grader evidence was given")
    parsed = _parse_sources(sources)
    records = list(parsed.records)
    if not records:
        # Still fails closed on a file with nothing to review -- an empty case
        # set that writes successfully is how a judge gets calibrated on zero
        # cases and nobody notices.
        detail = ", ".join(
            f"{reason} ({count})"
            for reason, count in sorted(skip_reason_counts(parsed.skipped).items())
        )
        raise CalibrationCaseError(
            f"no grader record is reviewable: {len(parsed.skipped)} skipped"
            + (f" -- {detail}" if detail else "")
        )

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
    input_sha256s = tuple(_sha256(source) for source in sources)
    case_set = CalibrationCaseSet(
        review_cases=tuple(review_cases),
        private_mapping=tuple(mapping),
        # A single file keeps the digest it always had; several are addressed
        # by their ordered per-file digests.
        input_sha256=(
            input_sha256s[0]
            if len(sources) == 1
            else _sha256("\n".join(input_sha256s).encode("utf-8"))
        ),
        key_fingerprint=_sha256(blind_key),
        skipped=parsed.skipped,
        eligible_count=len(records),
        input_sha256s=input_sha256s,
    )
    if transcripts is None:
        return case_set
    return attach_transcripts(case_set, transcripts)


def _message_text(content: Any) -> str:
    """Visible text of a message; thinking blocks and signatures are dropped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text"))
            for part in content
            if isinstance(part, dict)
            and part.get("type", "text") == "text"
            and part.get("text") is not None
        )
    return ""


def render_transcript(canonical: bytes) -> dict[str, Any]:
    """The tool calls and returns of one specialist transcript, for a reviewer."""
    envelope = json.loads(canonical)
    turns: list[dict[str, Any]] = []
    for message in envelope.get("messages") or []:
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            continue
        if message.get("type") == "ToolMessage":
            turns.append(
                {
                    "role": "tool",
                    "name": data.get("name"),
                    "status": data.get("status"),
                    "content": _message_text(data.get("content")),
                }
            )
            continue
        turns.append(
            {
                "role": "assistant",
                "text": _message_text(data.get("content")),
                "tool_calls": [
                    {"name": call.get("name"), "args": call.get("args")}
                    for call in data.get("tool_calls") or []
                    if isinstance(call, dict)
                ],
            }
        )
    return {
        "source": envelope.get("source"),
        "turns": turns,
        "tool_failures": envelope.get("tool_failures") or [],
    }


def transcript_digests(events: Sequence[Any]) -> list[str]:
    """Digests of the specialist transcripts a run's findings reference."""
    digests: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != "finding":
            continue
        payload = event.get("payload")
        ref = payload.get("evidence_artifact_ref") if isinstance(payload, dict) else None
        digest = ref.get("sha256") if isinstance(ref, dict) else None
        if isinstance(digest, str) and digest not in digests:
            digests.append(digest)
    return digests


def attach_transcripts(
    case_set: CalibrationCaseSet, transcripts: Mapping[str, bytes]
) -> CalibrationCaseSet:
    """Give each case the tool returns its evidence claims can be checked against.

    Without them a reviewer can judge whether a chain hangs together but not
    whether "peaked at 3.19%" is what Prometheus returned. A referenced
    transcript that is not in the store is counted, not fatal: the case is
    still reviewable for causal_chain, and the manifest says how many evidence
    judgments rest on missing returns. Runs that predate durable transcripts
    reference none at all and are counted separately.
    """
    attached = missing = without = 0
    cases = []
    for case in case_set.review_cases:
        review_input = case["review_input"]
        digests = transcript_digests(review_input["timeline_events"])
        rendered = []
        for digest in digests:
            canonical = transcripts.get(digest)
            if canonical is None:
                missing += 1
                continue
            if not hmac.compare_digest(_sha256(canonical), digest):
                raise CalibrationCaseError(
                    f"transcript {digest[:12]} does not match its digest"
                )
            rendered.append(render_transcript(canonical))
            attached += 1
        without += int(not digests)
        cases.append(
            {
                **case,
                "review_input": {
                    **review_input,
                    "specialist_transcripts": rendered,
                    "missing_transcripts": len(digests) - len(rendered),
                },
            }
        )
    return replace(
        case_set,
        review_cases=tuple(cases),
        transcripts_attached=attached,
        transcripts_missing=missing,
        cases_without_transcripts=without,
    )


def load_transcripts(directory: Path) -> dict[str, bytes]:
    """Read `<sha256>.json` canonical transcripts; a name that lies is fatal."""
    if not directory.is_dir():
        raise CalibrationCaseError(f"transcript store does not exist: {directory}")
    store: dict[str, bytes] = {}
    for path in sorted(directory.glob("*.json")):
        canonical = path.read_bytes()
        if not hmac.compare_digest(_sha256(canonical), path.stem):
            raise CalibrationCaseError(f"{path.name} does not match its digest")
        store[path.stem] = canonical
    return store


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
        # A reviewer comparing two manifests needs to see that the second one
        # covers fewer runs because the rubric moved, not because the agent
        # produced fewer of them.
        "eligible_records": case_set.eligible_count,
        "skipped_records": len(case_set.skipped),
        "skipped_reasons": skip_reason_counts(case_set.skipped),
        "input_sha256": case_set.input_sha256,
        "input_sha256s": list(case_set.input_sha256s),
        "transcripts_attached": case_set.transcripts_attached,
        "transcripts_missing": case_set.transcripts_missing,
        "cases_without_transcripts": case_set.cases_without_transcripts,
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
    parser.add_argument("grader_records", type=Path, nargs="+")
    parser.add_argument("--blind-key-file", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--private-mapping-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        help="store written by benchmarks.transcript_store; attaches tool returns",
    )
    args = parser.parse_args(argv)
    try:
        sources = [path.read_bytes() for path in args.grader_records]
        case_set = build_case_set(
            sources,
            blind_key=_load_key(args.blind_key_file),
            limit=args.limit,
            transcripts=(
                load_transcripts(args.transcript_dir)
                if args.transcript_dir is not None
                else None
            ),
        )
        write_case_set(
            case_set,
            review_path=args.review_output,
            mapping_path=args.private_mapping_output,
            manifest_path=args.manifest_output,
        )
        print(
            f"cases: {len(case_set.review_cases)} "
            f"(reviewable {case_set.eligible_count}, "
            f"skipped {len(case_set.skipped)})"
        )
        for reason, count in sorted(skip_reason_counts(case_set.skipped).items()):
            print(f"  skipped {count}: {reason}")
        if case_set.transcripts_attached is not None:
            print(
                f"transcripts: {case_set.transcripts_attached} attached, "
                f"{case_set.transcripts_missing} missing, "
                f"{case_set.cases_without_transcripts} cases reference none"
            )
    except (OSError, CalibrationCaseError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
