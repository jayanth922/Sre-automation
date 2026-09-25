#!/usr/bin/env python3
"""Unit tests for the github-real MCP server's payload shaping.

The module under test is deliberately dependency-free — ``server.py`` needs
PyGithub and ``mcp``, neither of which is installed in the platform venv — so
it is loaded by path, the same way ``test_github_exec_guardrails.py`` loads
``github_exec/guardrails.py``.
"""

import importlib.util
import sys
from datetime import timezone
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "edge_mcp_servers"
    / "mcp_servers"
    / "github_real"
    / "payload.py"
)
_spec = importlib.util.spec_from_file_location("github_real_payload", _MODULE_PATH)
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)

# The cap the shaped payload has to stay under, restated here rather than
# imported: src/sre_agent/context_compaction.py is the downstream authority and a
# silent drift between the two is exactly what this test should fail on.
DOWNSTREAM_TOOL_RESULT_CAP = 20_000


def _commit(files, **overrides):
    kwargs = dict(
        sha="a" * 40,
        message="fix: raise the connection pool ceiling",
        author={"name": "Dev", "email": "dev@example.com", "login": "dev"},
        timestamp="2026-09-19T10:00:00+00:00",
        url="https://github.com/org/repo/commit/" + "a" * 40,
        files=files,
        additions=12,
        deletions=3,
    )
    kwargs.update(overrides)
    return p.shape_commit(**kwargs)


def _file(name, changes, patch_chars):
    return {
        "filename": name,
        "status": "modified",
        "additions": changes,
        "deletions": 0,
        "changes": changes,
        "patch": "+" * patch_chars,
    }


def test_a_small_commit_keeps_every_file_and_every_patch():
    payload = _commit([_file("app/db.py", 9, 300), _file("README.md", 2, 40)])

    assert payload["files_changed"] == 2
    assert [row["filename"] for row in payload["files"]] == ["app/db.py", "README.md"]
    assert len(payload["files"][0]["patch"]) == 300
    # Nothing was dropped, so none of the loss flags appear and the caller is
    # not told to re-read something that is already complete.
    for key in ("files_omitted", "patch_chars_omitted", "note", "files_scan_truncated"):
        assert key not in payload


def test_the_patch_budget_is_spent_on_the_biggest_change_not_the_first_one():
    # The filenames are chosen so that sorting by name and sorting by change
    # size disagree: `app/...` wins alphabetically, `src/...` wins on the
    # 5000-line rewrite that plausibly caused the incident. A head-and-tail
    # cut downstream would keep the wrong one.
    payload = _commit(
        [
            _file("app/constants.py", 2, 60),
            _file("src/pool.py", 5_000, 50_000),
        ]
    )

    ordered = [row["filename"] for row in payload["files"]]
    assert ordered == ["src/pool.py", "app/constants.py"]
    assert len(payload["files"][0]["patch"]) == p.MAX_PATCH_CHARS_PER_FILE
    assert payload["files"][0]["patch_truncated"] is True
    # The small file still gets its patch: the per-file cap, not the total
    # budget, is what bit here.
    assert len(payload["files"][1]["patch"]) == 60


def test_a_file_that_loses_its_patch_keeps_its_row():
    # 40 files, each wanting the 2000-char per-file maximum, is far more
    # patch text than the payload budget can hold.
    payload = _commit([_file(f"svc/mod{i:02d}.py", 900 - i, 4_000) for i in range(40)])

    assert payload["files_changed"] == 40
    assert len(payload["files"]) == 40
    with_patch = [row for row in payload["files"] if row.get("patch")]
    # Some files get their diff and the rest run out of budget — the point is
    # that the split falls where the budget falls, in rank order.
    assert 0 < len(with_patch) < 40
    assert [row["filename"] for row in with_patch] == [
        row["filename"] for row in payload["files"][: len(with_patch)]
    ]
    # Every file is still named with its line counts — knowing *which* file
    # changed is most of the finding even when the diff text is gone.
    assert [row["filename"] for row in payload["files"]] == [
        f"svc/mod{i:02d}.py" for i in range(40)
    ]
    assert all("changes" in row for row in payload["files"])
    assert payload["patch_chars_omitted"] > 0
    assert "narrow the investigation" in payload["note"]


def test_a_commit_with_more_files_than_the_row_cap_reports_the_remainder():
    payload = _commit([_file(f"f{i:03d}.py", 1_000 - i, 10) for i in range(120)])

    assert payload["files_changed"] == 120
    assert len(payload["files"]) == p.MAX_FILES_REPORTED
    assert payload["files_omitted"] == 120 - p.MAX_FILES_REPORTED
    assert "note" in payload


def test_a_truncated_file_scan_says_the_count_is_a_lower_bound():
    payload = _commit([_file(f"f{i:03d}.py", 5, 10) for i in range(300)], scan_truncated=True)

    assert payload["files_scan_truncated"] is True
    # Without this the model reads `files_changed: 300` on a 3000-file commit
    # as the whole truth.
    assert payload["files_changed_is_lower_bound"] is True
    assert "note" in payload


def test_the_worst_case_payload_lands_under_the_downstream_tool_result_cap():
    # The scenario the shaping exists for: the largest commit the scan will
    # look at, every file maximally noisy, every filename long.
    import json

    payload = _commit(
        [
            _file(f"services/{'deep/' * 6}module_{i:04d}.py", 9_999 - i, 60_000)
            for i in range(p.MAX_FILES_SCANNED)
        ],
        message="squashed changelog\n" * 5_000,
        scan_truncated=True,
    )
    encoded = json.dumps(payload, separators=(",", ":"))

    assert len(encoded) < DOWNSTREAM_TOOL_RESULT_CAP, (
        f"shaped payload is {len(encoded)} chars; the head-and-tail elision in "
        "src/sre_agent/context_compaction.py would run and undo the ranking"
    )


def test_long_paths_eat_the_patch_budget_rather_than_overflowing_the_payload():
    # The other regime: the file rows themselves are the expensive part. A
    # fixed patch allowance would have added 12000 chars on top of ~16000 of
    # filenames and blown the cap; the budget has to come out of what the
    # rows left behind.
    import json

    payload = _commit(
        [_file(("deep/" * 40) + f"module_{i:03d}.py", 900 - i, 50_000) for i in range(300)],
        message="m" * 9_000,
        scan_truncated=True,
    )
    encoded = json.dumps(payload, separators=(",", ":"))

    assert len(encoded) < DOWNSTREAM_TOOL_RESULT_CAP
    # Rows are never sacrificed for patch text: all 50 reportable files are
    # named even when that leaves almost nothing for diffs.
    assert len(payload["files"]) == p.MAX_FILES_REPORTED
    assert payload["patch_chars_omitted"] > 0


def test_an_oversized_commit_message_is_capped_and_flagged():
    payload = _commit([_file("a.py", 1, 10)], message="x" * 9_000)

    assert len(payload["message"]) == p.MAX_MESSAGE_CHARS
    assert payload["message_truncated"] is True


def test_a_missing_patch_is_not_an_error():
    # Binary files and renames come back from GitHub with patch=None.
    payload = _commit([{"filename": "logo.png", "status": "modified", "changes": 0}])

    row = payload["files"][0]
    assert row["filename"] == "logo.png"
    assert "patch" not in row
    assert row["additions"] == 0


def test_a_pull_request_body_is_capped_and_flagged():
    shaped = p.shape_pull_request({"number": 7, "body": "y" * 9_000})

    assert shaped["number"] == 7
    assert len(shaped["body"]) == p.MAX_BODY_CHARS
    assert shaped["body_truncated"] is True

    short = p.shape_pull_request({"number": 8, "body": None})
    assert short["body"] == ""
    assert "body_truncated" not in short


def test_a_commit_summary_caps_its_message():
    shaped = p.shape_commit_summary({"sha": "abc", "message": "z" * 5_000})

    assert len(shaped["message"]) == p.MAX_MESSAGE_CHARS
    assert shaped["message_truncated"] is True


@pytest.mark.parametrize(
    "value,expected_hour",
    [
        ("2026-09-19T10:00:00Z", 10),
        ("2026-09-19T10:00:00+00:00", 10),
        ("2026-09-19T12:00:00+02:00", 10),
        ("2026-09-19T10:00:00", 10),  # naive is read as UTC
    ],
)
def test_parse_iso_normalises_to_utc(value, expected_hour):
    parsed = p.parse_iso(value)

    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == expected_hour


def test_parse_iso_treats_blank_as_no_bound():
    assert p.parse_iso(None) is None
    assert p.parse_iso("   ") is None


def test_parse_iso_refuses_to_silently_drop_a_malformed_bound():
    # Returning None here would hand `get_commits()` no `since`, which walks
    # the repository's entire history one page at a time — the defect this
    # module was written to close.
    with pytest.raises(ValueError):
        p.parse_iso("last tuesday")
