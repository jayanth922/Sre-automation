#!/usr/bin/env python3
"""Write specialist tool transcripts to a content-addressed review store.

Grader records keep only `evidence_artifact_ref` for each finding; the tool
calls and returns themselves live gzip-compressed in PostgreSQL's
`evidence_artifacts` table (see `sre_agent.evidence_artifacts`). A semantic
reviewer needs those returns to tell whether an evidence claim is what the tool
said, so this turns a `psql` export into `<sha256>.json` files that
`calibration_cases --transcript-dir` can attach to blinded cases.

Input is one row per line, `<content_sha256>|<base64 gzip payload>`, as
`psql -At -F "|"` prints it. The digest is over the canonical JSON before
compression -- the same digest the finding's reference carries -- so every
row is verified after decompression and a mismatch stops the write. It reads
the database only through that export and never calls a model, so building the
store has no API cost.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import hmac
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


class TranscriptStoreError(ValueError):
    """An exported transcript is not the content its digest names."""


@dataclass(frozen=True)
class StoreResult:
    written: int
    already_present: int


def _row(line: str, line_number: int) -> tuple[str, bytes]:
    digest, separator, encoded = line.strip().partition("|")
    if not separator:
        raise TranscriptStoreError(f"line {line_number} is not `<sha256>|<base64>`")
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
    return digest, canonical


def write_store(lines: Iterable[str], directory: Path) -> StoreResult:
    """Verify every row before writing any, so a bad export leaves no partial store."""
    rows: dict[str, bytes] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        digest, canonical = _row(line, line_number)
        rows[digest] = canonical
    if not rows:
        raise TranscriptStoreError("export contains no transcripts")
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
    return StoreResult(written=written, already_present=present)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", type=Path, help="directory for <sha256>.json files")
    args = parser.parse_args(argv)
    try:
        result = write_store(sys.stdin, args.store)
    except (OSError, TranscriptStoreError) as exc:
        parser.error(str(exc))
    print(f"transcripts: {result.written} written, {result.already_present} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
