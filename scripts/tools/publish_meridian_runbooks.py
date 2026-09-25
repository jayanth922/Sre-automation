#!/usr/bin/env python3
"""Publish the rewritten Meridian runbooks into the live Notion pages.

The four pages in `examples/meridian/runbooks/` score 22/22 on
`scripts/tools/audit_runbook_coverage.py` against the v2 scenarios, where the live
Notion bodies score 0/22. Until they are published, none of that is in effect:
the agent retrieves the live pages at run time.

**This writes to Notion and is outward-facing.** It is a dry run unless
`--confirm` is passed, and it refuses to write anything it cannot first prove
will survive the round trip.

What it checks before writing
-----------------------------
1. Every draft has an H1, and that H1 matches exactly one live page title.
   Title drift would orphan a rewrite, or worse, create a second page.
2. Every draft round-trips byte-for-byte through `_markdown_to_blocks` ->
   `_blocks_to_markdown`. If a draft cannot survive the block encoding, the
   agent would not read back what was audited.
3. With `--audit-corpus`, the drafts score 22/22 as an overlay first.

How it writes
-------------
`replace_notion_page_body`, not `upsert_notion_runbook`: the page keeps its
id, its URL, and every property. Archive-and-recreate would keep only the
properties this script happened to pass, and retrieval — verified at 22/22 —
scores against properties this script does not know about.

After a real publish, verify what Notion actually serves:

    python scripts/tools/dump_notion_runbook_corpus.py --cluster-id <uuid> --out /tmp/after.json
    python scripts/tools/audit_runbook_coverage.py --corpus /tmp/after.json

That, not the local 22/22, is the number that says Phase B is live.

Usage:
    python scripts/tools/publish_meridian_runbooks.py --cluster-id <uuid>
    python scripts/tools/publish_meridian_runbooks.py --cluster-id <uuid> --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from sre_agent.notion_runbooks import (  # noqa: E402
    _blocks_to_markdown,
    _markdown_to_blocks,
    list_notion_runbooks,
    replace_notion_page_body,
)

sys.path.insert(0, str(REPO / "scripts" / "tools"))
from dump_notion_runbook_corpus import _resolve_creds  # noqa: E402

DEFAULT_DRAFTS = REPO / "examples" / "meridian" / "runbooks"


def _title_of(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _notion_echo(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The blocks as Notion hands them back — with `plain_text` filled in."""
    out = []
    for block in blocks:
        kind = block["type"]
        inner = dict(block[kind])
        inner["rich_text"] = [
            {**rt, "plain_text": rt["text"]["content"]} for rt in inner.get("rich_text", [])
        ]
        out.append({**block, kind: inner})
    return out


def _round_trips(markdown: str) -> bool:
    encoded = _blocks_to_markdown(_notion_echo(_markdown_to_blocks(markdown)))
    return encoded.strip() == markdown.strip()


def _audit(corpus: Path, drafts: Path) -> Tuple[int, int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "tools" / "audit_runbook_coverage.py"),
            "--corpus", str(corpus),
            "--proposed", str(drafts),
            "--format", "json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        raise SystemExit(f"audit failed to run:\n{proc.stdout}\n{proc.stderr}")
    verdicts = json.loads(proc.stdout)
    passing = sum(1 for v in verdicts if v.get("passes"))
    return passing, len(verdicts), proc.stdout


async def run(args: argparse.Namespace) -> int:
    api_key, database_id = _resolve_creds(args)

    drafts: Dict[str, Path] = {}
    for path in sorted(args.drafts.glob("*.md")):
        title = _title_of(path.read_text())
        if not title:
            raise SystemExit(f"{path}: no '# Title' heading to match against Notion")
        drafts[title] = path

    live = await list_notion_runbooks(api_key, database_id)
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for record in live:
        by_title.setdefault(record["title"], []).append(record)

    problems: List[str] = []
    plan: List[Tuple[Path, str, str, int]] = []
    for title, path in drafts.items():
        body = path.read_text()
        matches = by_title.get(title, [])
        if len(matches) != 1:
            problems.append(
                f"{path.name}: {'no live page titled' if not matches else f'{len(matches)} live pages titled'} {title!r}"
            )
            continue
        if not _round_trips(body):
            problems.append(f"{path.name}: does not survive the Notion block round trip")
            continue
        plan.append((path, matches[0]["id"], title, len(_markdown_to_blocks(body))))

    if args.audit_corpus:
        passing, total, _ = _audit(args.audit_corpus, args.drafts)
        print(f"audit (drafts as overlay): {passing}/{total} scenarios pass")
        if passing < total:
            problems.append(
                f"drafts score {passing}/{total}; publish only what the audit passes"
            )

    print(f"\n{len(live)} live pages; {len(drafts)} drafts\n")
    for path, page_id, title, blocks in plan:
        chunks = -(-blocks // 100)
        print(f"  {path.name:38} -> {title}")
        print(f"  {'':38}    page {page_id}, {blocks} blocks, {chunks} append call(s)")

    if problems:
        print("\nREFUSING TO PUBLISH:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not args.confirm:
        print(
            "\nDry run. Nothing was written.\n"
            "Re-run with --confirm to replace these page bodies, then verify with:\n"
            "  python scripts/tools/dump_notion_runbook_corpus.py --out /tmp/after.json ...\n"
            "  python scripts/tools/audit_runbook_coverage.py --corpus /tmp/after.json"
        )
        return 0

    print("\npublishing...")
    for path, page_id, title, _ in plan:
        written = await replace_notion_page_body(api_key, page_id, path.read_text())
        print(f"  {title}: {written} blocks written")

    print(
        "\nPublished. This has NOT been verified — the publish succeeding and "
        "Notion serving the whole page are different claims. Dump the corpus "
        "and re-run the audit."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, default=DEFAULT_DRAFTS)
    parser.add_argument("--api-key")
    parser.add_argument("--database-id")
    parser.add_argument("--cluster-id", help="read credentials from the platform database")
    parser.add_argument(
        "--audit-corpus",
        type=Path,
        help="score the drafts against this corpus dump before publishing",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually write to Notion (default is a dry run)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
