#!/usr/bin/env python3
"""What the judge is shown of a review case: the claim and what it rests on.

Labellers read the whole case; the judge reads less. Perturbed copies change
only the structured block, so agent-written text that restates it -- the
narrator's prose (`summary_text`, the summary's `content`, `narrative`,
`hypothesis`, `raw_final_response`) and the block's own `timeline` and
`uncertainty` -- would let a judge find a perturbation by diffing that text
against the block, which no real failure allows. The rest of the timeline
(plans, routing decisions, remediation, alert reconciliation) is not what
either criterion grades.

The view keeps the alert the run was opened for, the claim (`diagnosis`,
`causal_chain`, `evidence`), each specialist's final response (written before
the narrator, so it cannot echo the block), every specialist tool call and
return, and whether any transcript is missing. `--sources` checks that a
perturbed copy's view differs from its source's only in the claim.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

VIEW_VERSION = 1
CLAIM_FIELDS = ("diagnosis", "causal_chain", "evidence")


class JudgeViewError(ValueError):
    pass


def _summary(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for event in reversed(events):
        payload = event.get("payload")
        if event.get("event_type") == "summary" and isinstance(payload, dict):
            return payload
    return {}


def judge_view(case: Mapping[str, Any]) -> dict[str, Any]:
    review_input = case.get("review_input") or {}
    events = [e for e in review_input.get("timeline_events") or [] if isinstance(e, dict)]
    evaluation = review_input.get("benchmark_evaluation")
    if not isinstance(evaluation, dict):
        raise JudgeViewError(f"{case.get('blind_case_id')}: no benchmark_evaluation")
    alert = _summary(events).get("alert_context")
    findings = []
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") == "finding" and isinstance(payload, dict):
            findings.append(
                {
                    "agent_name": payload.get("agent_name"),
                    "response": payload.get("raw_response"),
                    "tool_failures": payload.get("tool_failures") or [],
                }
            )
    return {
        "view_version": VIEW_VERSION,
        "blind_case_id": case.get("blind_case_id"),
        "alert": alert if isinstance(alert, str) else None,
        **{name: evaluation.get(name) for name in CLAIM_FIELDS},
        "findings": findings,
        "transcripts": review_input.get("specialist_transcripts") or [],
        "missing_transcripts": review_input.get("missing_transcripts") or 0,
        "unattached_sources": review_input.get("unattached_sources") or [],
    }


def leaked_fields(view: Mapping[str, Any], source_view: Mapping[str, Any]) -> list[str]:
    """Fields outside the claim where a perturbed view differs from its source's."""
    keys = (set(view) | set(source_view)) - set(CLAIM_FIELDS) - {"blind_case_id"}
    return sorted(k for k in keys if view.get(k) != source_view.get(k))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("review", type=Path, help="review.jsonl (real or perturbed)")
    parser.add_argument("--output", type=Path, required=True, help="judge views as JSONL")
    parser.add_argument(
        "--sources",
        nargs=2,
        type=Path,
        metavar=("SOURCE_REVIEW", "PERTURBED_PRIVATE_MAP"),
        help="check each perturbed view against its source case's view",
    )
    args = parser.parse_args(argv)
    try:
        views = [judge_view(case) for case in _read_jsonl(args.review)]
        if not views:
            raise JudgeViewError(f"no cases in {args.review}")
        if args.sources:
            sources = {c["blind_case_id"]: judge_view(c) for c in _read_jsonl(args.sources[0])}
            origin = {
                row["blind_case_id"]: row["source_blind_case_id"]
                for row in _read_jsonl(args.sources[1])
            }
            for view in views:
                source = sources.get(origin.get(view["blind_case_id"]))
                if source is None:
                    raise JudgeViewError(f"{view['blind_case_id']}: source case not found")
                leaked = leaked_fields(view, source)
                if leaked:
                    raise JudgeViewError(
                        f"{view['blind_case_id']}: differs from its source in {', '.join(leaked)}"
                    )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    lines = [json.dumps(v, sort_keys=True, ensure_ascii=False) for v in views]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    sizes = sorted(len(line) for line in lines)
    print(
        f"views: {len(views)}; characters min={sizes[0]} "
        f"median={int(statistics.median(sizes))} max={sizes[-1]} total={sum(sizes)}"
    )
    if args.sources:
        print("every view differs from its source only in " + ", ".join(CLAIM_FIELDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
