#!/usr/bin/env python3
"""Write specialist tool transcripts to a content-addressed review store.

Grader records keep only `evidence_artifact_ref` for each finding; the tool
calls and returns themselves live gzip-compressed in PostgreSQL's
`evidence_artifacts` table (see `sre_agent.evidence_artifacts`). A semantic
reviewer needs those returns to tell whether an evidence claim is what the tool
said, so this turns a `psql` export into `<sha256>.json` files that
`calibration_cases --transcript-dir` can attach to blinded cases.

Input is one row per line, as `psql -At -F "|"` prints it: either
`<content_sha256>|<base64 gzip payload>` or
`<content_sha256>|<root_trace_id>|<created_at>|<base64 gzip payload>`. The
digest is over the canonical JSON before compression -- the same digest the
finding's reference carries -- so every row is verified after decompression and
a mismatch stops the write. It reads the database only through that export and
never calls a model, so building the store has no API cost.

The four-field form also records each transcript's run in `index.jsonl`. Only
visible specialists post a finding that references their transcript; the
Kubernetes prescan informs the summary without one, so its calls and returns
can be found only by the root trace they share with the findings.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

INDEX_NAME = "index.jsonl"


class TranscriptStoreError(ValueError):
    """An exported transcript is not the content its digest names."""


@dataclass(frozen=True)
class StoreResult:
    written: int
    already_present: int
    # Rows in `index.jsonl` after this write; 0 when no row named its run.
    indexed: int = 0


@dataclass(frozen=True, order=True)
class TraceOrigin:
    """The run a stored transcript came from."""

    root_trace_id: str
    created_at: str
    sha256: str


def _row(line: str, line_number: int) -> tuple[str, bytes, Optional[TraceOrigin]]:
    fields = line.strip().split("|")
    if len(fields) == 2:
        digest, encoded = fields
        root_trace_id = created_at = ""
    elif len(fields) == 4:
        digest, root_trace_id, created_at, encoded = fields
    else:
        raise TranscriptStoreError(
            f"line {line_number} is not `<sha256>|<base64>` or "
            "`<sha256>|<root_trace_id>|<created_at>|<base64>`"
        )
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise TranscriptStoreError(f"line {line_number} digest must be lowercase SHA-256")
    try:
        canonical = gzip.decompress(base64.b64decode(encoded, validate=True))
    except (binascii.Error, OSError, EOFError) as exc:
        raise TranscriptStoreError(
            f"line {line_number} is not base64 gzip: {exc}"
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), digest):
        raise TranscriptStoreError(f"line {line_number} does not match its digest")
    root_trace_id = root_trace_id.strip()
    origin = (
        TraceOrigin(root_trace_id, created_at.strip(), digest) if root_trace_id else None
    )
    return digest, canonical, origin


def read_index(directory: Path) -> tuple[TraceOrigin, ...]:
    """The store's run index, sorted; empty when the store has none."""
    path = directory / INDEX_NAME
    if not path.exists():
        return ()
    origins = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            origin = TraceOrigin(
                str(row["root_trace_id"]), str(row["created_at"]), str(row["sha256"])
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise TranscriptStoreError(
                f"{INDEX_NAME} line {line_number} is malformed"
            ) from exc
        origins.add(origin)
    return tuple(sorted(origins))


def _write_index(directory: Path, origins: Iterable[TraceOrigin]) -> None:
    path = directory / INDEX_NAME
    staged = path.with_suffix(".jsonl.tmp")
    staged.write_text(
        "".join(
            json.dumps(
                {
                    "created_at": origin.created_at,
                    "root_trace_id": origin.root_trace_id,
                    "sha256": origin.sha256,
                },
                sort_keys=True,
            )
            + "\n"
            for origin in sorted(origins)
        ),
        encoding="utf-8",
    )
    os.replace(staged, path)


def write_store(lines: Iterable[str], directory: Path) -> StoreResult:
    """Verify every row before writing any, so a bad export leaves no partial store."""
    rows: dict[str, bytes] = {}
    origins: set[TraceOrigin] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        digest, canonical, origin = _row(line, line_number)
        rows[digest] = canonical
        if origin is not None:
            origins.add(origin)
    if not rows:
        raise TranscriptStoreError("export contains no transcripts")
    previous = read_index(directory) if directory.is_dir() else ()
    directory.mkdir(parents=True, exist_ok=True)
    written = present = 0
    for digest, canonical in sorted(rows.items()):
        path = directory / f"{digest}.json"
        if path.exists():
            if path.read_bytes() != canonical:
                raise TranscriptStoreError(f"{path.name} already holds other content")
            present += 1
            continue
        path.write_bytes(canonical)
        written += 1
    index = set(previous) | origins
    if origins:
        _write_index(directory, index)
    return StoreResult(written=written, already_present=present, indexed=len(index))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", type=Path, help="directory for <sha256>.json files")
    args = parser.parse_args(argv)
    try:
        result = write_store(sys.stdin, args.store)
    except (OSError, TranscriptStoreError) as exc:
        parser.error(str(exc))
    print(
        f"transcripts: {result.written} written, {result.already_present} already present, "
        f"{result.indexed} indexed by run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
