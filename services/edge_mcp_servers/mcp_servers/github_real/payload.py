#!/usr/bin/env python3
"""Bound what a GitHub read puts in front of the model.

Dependency-free on purpose. ``server.py`` needs PyGithub and ``mcp``, neither
of which is installed in the platform venv, so the shaping logic lives here
where tests can import it — the same split as ``github_exec/guardrails.py``.

Two defects motivate this module.

**A commit's diff never arrived.** ``handle_get_commit`` read
``commit.patch if hasattr(commit, "patch") else None``. ``github.Commit.Commit``
has no ``patch`` property (its properties are author, comments_url, commit,
committer, files, html_url, node_id, parents, repository, sha, stats,
text_matches) and ``GithubObject`` defines no ``__getattr__`` fallback, so that
expression is ``None`` on every call. The tool the GitHub agent is told returns
"detailed commit information including diff" returned ``"diff":null``, while
``list(commit.files)`` — the paginated call that does hold the patch text — was
walked and then thrown away except for its length.

**The other direction is just as bad.** The patch data that *is* available is
unbounded: GitHub serves up to 3000 files per commit and a refactor's diff runs
to hundreds of kilobytes. Handing that to the model would blow straight past the
20,000-character tool-result cap in ``src/sre_agent/context_compaction.py``, whose
head-and-tail elision would then keep the alphabetically first and last hunks —
an ordering with no relationship to which file caused the incident.

So the payload is shaped here instead of being cut downstream: up to fifty
filename/stat rows survive, omitted rows are counted, and the patch budget is
spent largest-change-first. The result is sized to land under the downstream
cap so that the structure, not a byte offset, decides what the model sees.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# A commit message can carry a squashed changelog; a PR body can carry a
# template. Both are worth reading and neither is worth a page.
MAX_MESSAGE_CHARS = 2_000
MAX_BODY_CHARS = 4_000

# Stats are cheap and identifying, so many more files keep their row than keep
# their patch. `files_omitted` reports the remainder rather than hiding it.
MAX_FILES_REPORTED = 50

# `Commit.files` is a paginated property: 300 entries per page, up to 3000
# files, one HTTP round trip per page. Reading one page bounds the edge
# server's own cost as well as the payload. A commit touching more than 300
# files is not one an incident read should be summarising anyway, and the
# response says so rather than pretending the scan was complete.
MAX_FILES_SCANNED = 300

# `list_commits` filters by author in this process, because GitHub's
# server-side `author` is an exact match and the tool's contract is a
# substring one. A filter that matches nothing must not page through the
# whole window looking for it.
MAX_COMMITS_SCANNED = 200

# The whole *encoded* payload must fit under DEFAULT_TOOL_RESULT_MAX_CHARS
# (20,000) in src/sre_agent/context_compaction.py, or the elision this module
# exists to avoid runs anyway and takes the ranking with it. 18,000 leaves
# headroom for the transport envelope.
#
# The patch budget is therefore not a constant: it is whatever is left of
# MAX_PAYLOAD_CHARS once the message and the file rows are encoded, measured
# on the JSON rather than on the raw strings. A diff escapes to more than its
# own length (every newline becomes two characters), and a repo with deep
# paths spends far more on filenames than a flat one, so a fixed total was
# wrong in both directions — it overflowed on long paths and left tokens on
# the table on short ones.
MAX_PAYLOAD_CHARS = 18_000
MAX_PATCH_CHARS_PER_FILE = 2_000

# The same contract as the transcript's elision marker in
# src/sre_agent/context_compaction.py: what is absent here is absent from this
# view only. A model concluding "that file was not touched" from a payload
# that told it rows were omitted is the failure this sentence exists to
# prevent.
TRUNCATION_NOTE = (
    "Largest changes first. Omitted rows and patch text are not "
    "absent from the commit — narrow the investigation to a path and "
    "re-read rather than concluding a file was unchanged."
)

# Space held back before the patches are laid in, for the keys that spending
# the patch budget can itself add to the payload.
_RESERVED_FOR_LOSS_KEYS = (
    len(',"patch_chars_omitted":') + 12 + len(',"note":') + len(json.dumps(TRUNCATION_NOTE))
)

# Cost of attaching a patch to one file row: the `,"patch":` key, plus
# `,"patch_truncated":true` for the clipped case, charged to every file so a
# later clip cannot overrun.
_PATCH_KEY_COST = len(',"patch":') + len(',"patch_truncated":true')


def cap_text(text: Optional[str], limit: int) -> Tuple[str, bool]:
    """Return ``text`` clipped to ``limit``, and whether clipping happened."""
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 instant into an aware UTC datetime.

    Raises ``ValueError`` on anything it cannot parse. Returning ``None`` for a
    malformed bound would be worse than failing: the caller would drop the
    filter and walk the repository's whole history instead.
    """
    text = (value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _encoded_len(value: Any) -> int:
    """Length of ``value`` as this module will actually serialise it."""
    return len(json.dumps(value, separators=(",", ":")))


def _fit_prefix(text: str, budget: int) -> str:
    """Longest prefix of ``text`` whose encoded cost fits ``budget``.

    Encoded cost, not length: ``"a\\nb"`` is three characters and encodes to
    six. Binary search because the expansion is monotonic in the prefix
    length but not uniform across it — one run of newlines or quotes early in
    a hunk would throw off any ratio estimate.
    """
    if budget <= 0 or not text:
        return ""
    if _encoded_len(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _encoded_len(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _file_row(raw: Mapping[str, Any]) -> Dict[str, Any]:
    def _int(key: str) -> int:
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "filename": str(raw.get("filename") or ""),
        "status": str(raw.get("status") or ""),
        "additions": _int("additions"),
        "deletions": _int("deletions"),
        "changes": _int("changes"),
        "patch": raw.get("patch") or "",
    }


def shape_commit(
    *,
    sha: str,
    message: Optional[str],
    author: Mapping[str, Any],
    timestamp: str,
    url: str,
    files: Sequence[Mapping[str, Any]],
    additions: int,
    deletions: int,
    scan_truncated: bool = False,
) -> Dict[str, Any]:
    """Build the bounded commit payload the GitHub agent actually needs.

    Files are ordered by how much they changed, not by name, and the patch
    budget is spent in that order: the agent's job is to name the change that
    plausibly caused the incident, so the largest change is the one whose text
    is worth the tokens. Within the reported-row cap, files past the patch
    budget keep their row and lose only their patch; rows past the cap are
    counted in ``files_omitted``.

    ``scan_truncated`` says the caller stopped reading pages of ``files``
    before the commit ran out of them, so both ``files_changed`` and the
    ranking describe the scanned prefix only. It is reported, never hidden:
    a ranking over a prefix is exactly the kind of thing a model will
    otherwise read as complete.
    """
    rows = [_file_row(entry) for entry in files]
    total_files = len(rows)
    ranked = sorted(rows, key=lambda row: row["changes"], reverse=True)
    reported = ranked[:MAX_FILES_REPORTED]

    # Rows first, patches second. Everything except the patch text is
    # mandatory — the model must be able to see every filename it is allowed
    # to see — so the rows are built, encoded, and the leftover space is what
    # the patches get to spend.
    patches: List[str] = []
    shaped_files: List[Dict[str, Any]] = []
    for row in reported:
        patches.append(row.pop("patch"))
        shaped_files.append(row)

    capped_message, message_truncated = cap_text(message, MAX_MESSAGE_CHARS)
    payload: Dict[str, Any] = {
        "sha": sha,
        "message": capped_message,
        "author": dict(author),
        "timestamp": timestamp,
        "url": url,
        "files_changed": total_files,
        "additions": additions,
        "deletions": deletions,
        "files": shaped_files,
    }
    if message_truncated:
        payload["message_truncated"] = True
    omitted = total_files - len(shaped_files)
    if omitted:
        payload["files_omitted"] = omitted
    if scan_truncated:
        payload["files_scan_truncated"] = True
        payload["files_changed_is_lower_bound"] = True

    # Reserve room for the keys the patch spending itself may add, so adding
    # them afterwards cannot push the payload back over the cap.
    reserved = _RESERVED_FOR_LOSS_KEYS
    remaining = MAX_PAYLOAD_CHARS - _encoded_len(payload) - reserved

    patch_chars_omitted = 0
    for row, patch in zip(shaped_files, patches):
        if not patch:
            continue
        # `,"patch":` plus, if it is clipped, `,"patch_truncated":true`.
        allowance = remaining - _PATCH_KEY_COST
        kept = _fit_prefix(patch[:MAX_PATCH_CHARS_PER_FILE], allowance)
        if kept:
            row["patch"] = kept
            remaining -= _PATCH_KEY_COST + _encoded_len(kept)
        if len(kept) < len(patch):
            patch_chars_omitted += len(patch) - len(kept)
            if kept:
                row["patch_truncated"] = True

    for row in ranked[MAX_FILES_REPORTED:]:
        patch_chars_omitted += len(row["patch"])

    if patch_chars_omitted:
        payload["patch_chars_omitted"] = patch_chars_omitted
    if omitted or patch_chars_omitted or scan_truncated:
        # The same contract as the transcript's elision marker: what is absent
        # here is absent from this view only. Inferring "the file was not
        # touched" from a payload that says it omitted rows is the failure
        # this note exists to prevent.
        payload["note"] = TRUNCATION_NOTE
    return payload


def shape_pull_request(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Cap a pull request's free-text body; everything else is already small."""
    payload = dict(raw)
    body, truncated = cap_text(payload.get("body"), MAX_BODY_CHARS)
    payload["body"] = body
    if truncated:
        payload["body_truncated"] = True
    return payload


def shape_commit_summary(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Cap a commit message in a list response.

    ``list_commits`` returns up to twenty of these; one squashed changelog in
    the set is enough to dominate the response.
    """
    payload = dict(raw)
    message, truncated = cap_text(payload.get("message"), MAX_MESSAGE_CHARS)
    payload["message"] = message
    if truncated:
        payload["message_truncated"] = True
    return payload
