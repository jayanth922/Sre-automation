#!/usr/bin/env python3
"""Deterministic `locatable` pre-check for evidence_support.

An evidence entry's `reference` names the tool call it rests on, for example
`list_events (involved_object=pod-x, namespace=meridian, limit=50) — Warning/…`.
Whether that call was made is mechanical: no judge should spend a token on it,
and no judge should be trusted to notice a reference the agent made up.

The check only ever fails an entry it can prove unlocatable: a tool from the MCP
catalog that was never called, or every call to the named tool disagrees on an
argument the reference names. A reference in prose, a name outside the catalog
(`payment_provider_up (service=…)` is a metric, not a call), a case without
transcripts, or one with transcripts missing from the store is UNCHECKABLE and
left to the judge. A renamed or omitted argument is tolerated, since the agent
may name a default it never passed; whether the claim is faithful to the return
is the judge's question.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

EntryState = Literal["LOCATED", "UNLOCATABLE", "UNCHECKABLE"]
CaseState = Literal["PASS", "FAIL", "UNCHECKABLE"]

_TOOL = re.compile(r"\s*`?([A-Za-z_][\w.\-]*)`?\s*\(")
_KEY = re.compile(r"[A-Za-z_]\w*")
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = set(_OPEN.values())
_MCP_SERVERS = Path(__file__).resolve().parents[2] / "services" / "edge_mcp_servers" / "mcp_servers"
_REGISTERED = re.compile(r"@mcp\.tool\([^)]*\)\s*\n\s*(?:async\s+)?def\s+(\w+)")


def tool_catalog(servers: Path = _MCP_SERVERS) -> frozenset[str]:
    """Every tool the edge MCP servers register; empty if the tree is absent."""
    return frozenset(
        name
        for path in sorted(servers.glob("*/server.py"))
        for name in _REGISTERED.findall(path.read_text(encoding="utf-8"))
    )


@dataclass(frozen=True)
class ToolCallRef:
    """The tool call an evidence reference claims, as the agent wrote it."""

    tool: str
    args: Mapping[str, str]


@dataclass(frozen=True)
class EntryCheck:
    index: int
    state: EntryState
    detail: str


@dataclass(frozen=True)
class CaseCheck:
    state: CaseState
    entries: tuple[EntryCheck, ...]
    detail: str

    def counts(self) -> dict[str, int]:
        out = {"LOCATED": 0, "UNLOCATABLE": 0, "UNCHECKABLE": 0}
        for entry in self.entries:
            out[entry.state] += 1
        return out


def _scan(text: str, start: int, stop_at_comma: bool) -> int:
    """Index of the first top-level `)` (or `,`) from `start`, or -1."""
    depth: list[str] = []
    quote: Optional[str] = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in _OPEN:
            depth.append(_OPEN[ch])
        elif ch in _CLOSE:
            if not depth:
                return i if ch == ")" else -1
            if depth[-1] == ch:
                depth.pop()
        elif ch == "," and stop_at_comma and not depth:
            return i
        i += 1
    return -1


def _split_args(inner: str) -> list[str]:
    parts: list[str] = []
    start = 0
    while True:
        end = _scan(inner + ")", start, stop_at_comma=True)
        if end < 0 or end >= len(inner):
            parts.append(inner[start:])
            return parts
        parts.append(inner[start:end])
        start = end + 1


def parse_reference(reference: Any) -> Optional[ToolCallRef]:
    """The `tool (key=value, …)` a reference opens with, if it opens with one.

    Tokens without `=` ("no path filter") are prose and ignored. Values keep
    their nested parentheses, braces and commas, so a PromQL query survives.
    """
    if not isinstance(reference, str):
        return None
    match = _TOOL.match(reference)
    if not match:
        return None
    close = _scan(reference, match.end(), stop_at_comma=False)
    if close < 0:
        return None
    args: dict[str, str] = {}
    for part in _split_args(reference[match.end():close]):
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and _KEY.fullmatch(key):
            args[key] = value.strip()
    return ToolCallRef(match.group(1), args)


def _norm(value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1]
    return re.sub(r"\s+", "", text.replace("'", '"')).casefold()


def _instant(text: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(text.replace("z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _same(claimed: str, actual: Any) -> bool:
    left, right = _norm(claimed), _norm(actual)
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except ValueError:
        pass
    a, b = _instant(left), _instant(right)
    return a is not None and b is not None and a == b


def _named(call_name: Any, tool: str) -> bool:
    if not isinstance(call_name, str):
        return False
    return call_name == tool or any(
        call_name.endswith(sep + tool) for sep in ("__", ".", "/")
    )


def tool_calls(transcripts: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict]]:
    """Every (name, args) the specialists called, from rendered transcripts."""
    calls = []
    for transcript in transcripts:
        for turn in transcript.get("turns") or []:
            if turn.get("role") != "assistant":
                continue
            for call in turn.get("tool_calls") or []:
                args = call.get("args")
                calls.append((call.get("name"), args if isinstance(args, dict) else {}))
    return calls


def check_entry(
    index: int,
    entry: Any,
    calls: Sequence[tuple[str, dict]],
    *,
    transcripts_complete: bool,
    catalog: frozenset[str],
) -> EntryCheck:
    reference = entry.get("reference") if isinstance(entry, dict) else None
    ref = parse_reference(reference)
    if ref is None:
        return EntryCheck(index, "UNCHECKABLE", "reference names no tool call")
    same_tool = [args for name, args in calls if _named(name, ref.tool)]
    if not same_tool:
        if not any(_named(ref.tool, tool) for tool in catalog):
            return EntryCheck(index, "UNCHECKABLE", f"{ref.tool} is not a known tool")
        verdict = f"no recorded call to {ref.tool}"
    else:
        for args in same_tool:
            if all(_same(v, args[k]) for k, v in ref.args.items() if k in args):
                return EntryCheck(index, "LOCATED", f"matches a {ref.tool} call")
        shared = sorted({k for args in same_tool for k in args} & set(ref.args))
        verdict = (
            f"every {ref.tool} call ({len(same_tool)}) disagrees on "
            + ", ".join(f"{k}={ref.args[k]}" for k in shared)
        )
    if not transcripts_complete:
        return EntryCheck(index, "UNCHECKABLE", verdict + "; some transcripts are missing")
    return EntryCheck(index, "UNLOCATABLE", verdict)


def check_case(
    case: Mapping[str, Any], catalog: Optional[frozenset[str]] = None
) -> CaseCheck:
    """The locatable verdict for one review case built with `--transcript-dir`."""
    if catalog is None:
        catalog = tool_catalog()
    review_input = case.get("review_input") or {}
    evaluation = review_input.get("benchmark_evaluation") or {}
    evidence = evaluation.get("evidence") if isinstance(evaluation, dict) else None
    if not isinstance(evidence, list) or not evidence:
        return CaseCheck("UNCHECKABLE", (), "no evidence entries")
    transcripts = review_input.get("specialist_transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        entries = tuple(
            EntryCheck(i, "UNCHECKABLE", "no transcripts attached")
            for i in range(len(evidence))
        )
        return CaseCheck("UNCHECKABLE", entries, "no transcripts attached")
    complete = not review_input.get("missing_transcripts")
    calls = tool_calls(transcripts)
    entries = tuple(
        check_entry(i, entry, calls, transcripts_complete=complete, catalog=catalog)
        for i, entry in enumerate(evidence)
    )
    unlocatable = [e for e in entries if e.state == "UNLOCATABLE"]
    if unlocatable:
        return CaseCheck(
            "FAIL",
            entries,
            "; ".join(f"evidence[{e.index}]: {e.detail}" for e in unlocatable),
        )
    if all(e.state == "UNCHECKABLE" for e in entries):
        return CaseCheck("UNCHECKABLE", entries, "no entry names a checkable tool call")
    return CaseCheck("PASS", entries, "every checkable reference matches a tool call")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("review", type=Path, help="review.jsonl from calibration_cases")
    parser.add_argument("--output", type=Path, help="per-case results as JSONL")
    args = parser.parse_args(argv)
    try:
        lines = args.review.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        parser.error(str(exc))
    catalog = tool_catalog()
    if not catalog:
        parser.error(f"no MCP tool catalog under {_MCP_SERVERS}")
    rows = []
    totals: dict[str, int] = {}
    entry_totals = {"LOCATED": 0, "UNLOCATABLE": 0, "UNCHECKABLE": 0}
    for line in lines:
        if not line.strip():
            continue
        case = json.loads(line)
        result = check_case(case, catalog)
        totals[result.state] = totals.get(result.state, 0) + 1
        for state, count in result.counts().items():
            entry_totals[state] += count
        rows.append(
            {
                "blind_case_id": case.get("blind_case_id"),
                "locatable": result.state,
                "detail": result.detail,
                "entries": [
                    {"index": e.index, "state": e.state, "detail": e.detail}
                    for e in result.entries
                ],
            }
        )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    print(
        f"cases: {len(rows)} "
        + " ".join(f"{state}={totals.get(state, 0)}" for state in ("PASS", "FAIL", "UNCHECKABLE"))
    )
    print("entries: " + " ".join(f"{k}={v}" for k, v in entry_totals.items()))
    for row in rows:
        if row["locatable"] == "FAIL":
            print(f"  {row['blind_case_id']}: {row['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
