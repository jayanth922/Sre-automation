#!/usr/bin/env python3
"""Replay runbook retrieval for every v2 benchmark scenario and grade the hit.

Two questions, and they fail independently:

  1. Retrieval — does the alert reach the runbook a human would have picked?
  2. Prescriptiveness — once there, does the runbook actually tell the agent
     what to do, or does it leave the decision to the model?

Question 2 is the one that matters. A runbook that is retrieved perfectly and
then says "investigate the downstream dependency" has not removed any reasoning
from the model; it has only moved it. This script grades each retrieved runbook
against the scenario's own contract — the actions the harness will accept, the
ones it will penalise, and the recovery probe it will use to decide whether the
incident actually recovered — and reports the gap per scenario.

The retrieval half reimplements the Notion MCP server's scoring
(`_compose_query` / `_score_record` in
services/edge_mcp_servers/mcp_servers/runbooks_notion/server.py) over a corpus dump
rather than calling Notion, so the audit runs offline and deterministically.
Keep the two in sync: if the server's scoring changes, this drifts silently.

What this does NOT measure: routing. The grader finds a branch that satisfies
the scenario's contract; it cannot tell that the runbook's decision procedure
would actually send this fault to that branch. Several acting branches fit the
same contract by letter -- "restart, and do not scale" suits a provider outage
and a bad deploy alike -- so a page whose Step 3 test is wrong still scores as
prescriptive here. Only a live run exercises routing. `_root_cause_affinity`
makes the reported branch the semantically likely one so the table is worth
reading, but that is presentation, not verification.

Run scripts/tools/audit_runbook_controls.py after changing this file. A pass count
means nothing unless the grader fails when the property it claims to measure
is deleted; that harness checks exactly that, and caught an earlier version of
this script reporting 22/22 while blind to a removed prohibition.

Usage:
    python scripts/tools/audit_runbook_coverage.py --corpus <dump.json>
    python scripts/tools/audit_runbook_coverage.py --corpus <dump.json> --format json

The corpus dump is a JSON list of runbook records, each with at least
`runbook_id`, `title`, `service`, `incident_type`, `severity`, `owner_team`,
`tags`, `alert_name` and `content`. See docs/ai/DECISIONS.md, "The runbook
reaches the agent as a rendered procedure, not as a search hit".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from sre_agent.runbook_brief import select_runbook  # noqa: E402

DATASET_DIR = REPO_ROOT / "evals" / "benchmarks" / "datasets" / "v2"
SPLITS = ("train", "dev", "holdout")


# ── The Notion server's scoring, reproduced ────────────────────────────────


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def _tokenize(query: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9_\-]+", query.lower()) if token]


def _search_blob(rb: Dict[str, Any]) -> str:
    fields = (
        "title",
        "service",
        "incident_type",
        "severity",
        "status",
        "owner_team",
        "tags",
        "alert_name",
        "impacted_environment",
    )
    return _normalize(" ".join(str(rb.get(f, "")) for f in fields))


def _score_record(rb: Dict[str, Any], query: str) -> float:
    normalized_query = _normalize(query)
    blob = _search_blob(rb)
    score = 0.0
    if not normalized_query:
        score += 1.0
    if normalized_query and normalized_query in _normalize(rb.get("title", "")):
        score += 8.0
    if normalized_query and normalized_query in _normalize(rb.get("service", "")):
        score += 4.0
    if normalized_query and normalized_query in _normalize(rb.get("incident_type", "")):
        score += 4.0
    for token in _tokenize(query):
        if token in blob:
            score += 1.0
    return score


def _compose_query(*, severity: str = "", service: str = "", alert_name: str = "") -> str:
    """What src/sre_agent/context_builder.py sends as the search arguments."""
    return " ".join(part for part in (severity, service, alert_name) if part).strip()


def search(corpus: Sequence[Dict[str, Any]], query: str, limit: int = 5) -> List[Dict[str, Any]]:
    scored = [(_score_record(rb, query), rb) for rb in corpus]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [dict(rb, score=score) for score, rb in scored[:limit] if score > 0]


# ── Grading a runbook against a scenario's contract ────────────────────────

# Action vocabulary as the harness names it, mapped to the phrasings a runbook
# would plausibly use. The MCP tool names are listed first: a runbook that
# names the tool the agent will actually call is more prescriptive than one
# naming a kubectl command the agent has no shell to run.
_ACTION_PHRASES: Dict[str, tuple] = {
    "rollback": ("rollback_deployment", "rollout undo", "roll back", "rollback", "previous revision"),
    "revert_commit": ("revert the commit", "revert any", "git revert", "revert"),
    "restart": ("restart_deployment", "recreate_pod", "rollout restart", "restart", "recreate the pod"),
    "scale": ("scale_deployment", "scale", "--replicas", "replicas="),
    "patch": ("patch_resource_limits", "patch"),
    "config_change": (
        "patch_deployment_env", "config change", "change config", "configmap", "pool size",
    ),
    "escalate": ("escalate", "page the", "hand off", "on-call"),
}

# A prohibition has to be phrased as one. "Consider whether to roll back" is not
# a prohibition; "do not roll back" is.
_PROHIBITION_MARKERS = (
    "do not",
    "don't",
    "never",
    "must not",
    "avoid",
    "is forbidden",
    "not appropriate",
    "will not help",
    "does not help",
    "no not",
)


def _sentences(body: str) -> List[str]:
    """Sentence-ish spans. Markdown bullets and numbered steps are their own
    units, so a newline ends a span just as a full stop does."""
    return [s for s in re.split(r"(?<=[.!?\n])", body.lower()) if s.strip()]


def _classify(body: str, action: str) -> tuple:
    """(is instructed anywhere, is prohibited anywhere).

    Both can be true at once, and for a branching runbook they usually are:
    scale is the correct action when a dependency is merely slow and the wrong
    one when it is failing. Grading the document as a whole — "mentioned, and
    not prohibited anywhere" — collapses that distinction and marks a runbook
    down for being precise. Each span is judged on its own.
    """
    phrases = _ACTION_PHRASES.get(action, (action,))
    instructed = prohibited = False
    for sentence in _sentences(body):
        if not any(phrase in sentence for phrase in phrases):
            continue
        if any(marker in sentence for marker in _PROHIBITION_MARKERS):
            prohibited = True
        else:
            instructed = True
    # Within one branch a prohibition governs the whole branch. "Do not revert
    # a commit. There is nothing to revert and the revert will not clear the
    # errors." is one prohibition with its reason attached, not a ban followed
    # by an instruction — and a runbook should not have to omit the reason to
    # score well, since the reason is what makes the rule survive paraphrase.
    return (instructed and not prohibited), prohibited


def _mentions(body: str, phrases: Iterable[str]) -> bool:
    lowered = body.lower()
    return any(phrase in lowered for phrase in phrases)


def _sections(body: str) -> List[tuple]:
    """(heading, text) for each `## ` section, plus a leading preamble.

    Grading has to happen inside a section, not across the document. A
    branching runbook contains a no-action branch whose blanket "do not
    restart, roll back, revert, scale, patch or change config" would otherwise
    satisfy the prohibition requirement for every acting scenario too — so a
    runbook could lose the prohibition from the branch that needs it and still
    score full marks. That is exactly the failure this audit exists to catch.
    """
    parts: List[tuple] = []
    heading = "(preamble)"
    buffer: List[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            parts.append((heading, "\n".join(buffer)))
            heading = line[3:].strip()
            buffer = []
        else:
            buffer.append(line)
    parts.append((heading, "\n".join(buffer)))
    return parts


@dataclass
class Verdict:
    scenario: str
    split: str
    alert: str
    service: str
    runbook: str
    runbook_id: str
    is_auto_generated: bool
    allowed: List[str]
    forbidden: List[str]
    prescribes: List[str] = field(default_factory=list)
    missing_prescription: List[str] = field(default_factory=list)
    prohibits: List[str] = field(default_factory=list)
    missing_prohibition: List[str] = field(default_factory=list)
    has_stop_condition: bool = False
    has_probe_query: bool = False
    branch: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def is_no_action(self) -> bool:
        return not self.allowed

    @property
    def passes(self) -> bool:
        if self.is_auto_generated:
            return False
        if self.missing_prohibition:
            return False
        if not self.has_probe_query:
            return False
        if self.is_no_action:
            return self.has_stop_condition
        # One named action is enough. The benchmark grades an action by
        # membership -- `if action_type not in allowed` in
        # evals/benchmarks/structured_grading.py:244 -- so naming one permitted
        # action is what makes the agent pass. `allowed_action_types` is a
        # permission set, not a checklist, and demanding every entry would
        # reward a runbook that lists options over one that prescribes a fix.
        # Prohibitions stay all-or-nothing: each forbidden action is a trap
        # the runbook exists to close.
        return bool(self.prescribes)


def grade(scenario: Dict[str, Any], split: str, hit: Dict[str, Any]) -> Verdict:
    body = str(hit.get("content") or "")
    alert = scenario["alert"]
    allowed = list(scenario.get("allowed_action_types") or [])
    forbidden = list(scenario.get("forbidden_action_types") or [])
    probe = scenario.get("recovery_probe") or {}

    verdict = Verdict(
        scenario=scenario["id"],
        split=split,
        alert=alert["alertname"],
        service=alert["service"],
        runbook=str(hit.get("title") or ""),
        runbook_id=str(hit.get("runbook_id") or ""),
        is_auto_generated=str(hit.get("runbook_id") or "").upper().startswith("RB-AUTO")
        or str(hit.get("title") or "").upper().startswith("RB-AUTO"),
        allowed=allowed,
        forbidden=forbidden,
    )

    # Find the branch that serves this scenario: one section that instructs at
    # least one permitted action (or, for a no-action scenario, carries the
    # stop condition) and prohibits every forbidden one. Scoring the union of
    # all sections would let a prohibition written for a different branch
    # cover a gap in this one.
    best: Dict[str, Any] | None = None
    for heading, section in _sections(body):
        instructed = [a for a in allowed if _classify(section, a)[0]]
        prohibited = [a for a in forbidden if _classify(section, a)[1]]
        wrongly_instructed = [a for a in forbidden if _classify(section, a)[0]]
        stop = _has_stop_condition(section)

        serves = stop if not allowed else bool(instructed)
        if not serves or wrongly_instructed:
            continue
        candidate = {
            "heading": heading,
            "instructed": instructed,
            "prohibited": prohibited,
            "missing": [a for a in forbidden if a not in prohibited],
            "stop": stop,
            "affinity": _root_cause_affinity(heading, section, scenario),
        }
        # Prefer the branch whose text is about this scenario's root cause,
        # then the one that leaves the fewest prohibitions unstated, then the
        # one that names the most permitted actions. Affinity leads because
        # several acting branches can satisfy the same contract by letter --
        # "restart and do not scale" fits a provider outage and a bad deploy
        # alike -- and the table is only worth reading if it names the branch
        # the runbook actually intends for this fault.
        key = lambda c: (-c["affinity"], len(c["missing"]), -len(c["instructed"]))
        if best is None or key(candidate) < key(best):
            best = candidate

    if best is None:
        verdict.missing_prescription = list(allowed)
        verdict.missing_prohibition = list(forbidden)
        verdict.notes.append("no single branch both prescribes and constrains this scenario")
    else:
        verdict.branch = best["heading"]
        verdict.prescribes = best["instructed"]
        verdict.missing_prescription = [a for a in allowed if a not in best["instructed"]]
        verdict.prohibits = best["prohibited"]
        verdict.missing_prohibition = best["missing"]
        verdict.has_stop_condition = best["stop"]

    # Verification is only checkable if the runbook carries the probe query the
    # harness will actually run. A prose "wait 15 minutes" is not verification.
    query = str(probe.get("query") or "")
    if query:
        # Compare on the metric name — whitespace and label order vary.
        needle = _core_metric(query).lower()
        # The metric has to appear where the pass condition lives, not just
        # anywhere in the page. Naming `payment_provider_up` in a background
        # note tells the agent the metric exists; it does not tell it what
        # reading counts as recovered, which is the part it would otherwise
        # have to invent. Require a digit in the same section for the same
        # reason: a probe with no threshold is not a verification step.
        verdict.has_probe_query = bool(needle) and any(
            needle in section.lower() and re.search(r"\d", section)
            for heading, section in _sections(body)
            if "verif" in heading.lower()
        )

    if verdict.is_auto_generated:
        verdict.notes.append("retrieval landed on an auto-generated post-incident page")
    if verdict.is_no_action and verdict.prescribes:
        verdict.notes.append("runbook prescribes action for a no-action scenario")

    return verdict


def _root_cause_affinity(heading: str, section: str, scenario: Dict[str, Any]) -> int:
    """How many of the scenario's root-cause keywords this branch talks about.

    The keywords come from the dataset (`root_cause.keywords`), not from the
    runbooks, so this ranks branches against an independent statement of what
    the fault is rather than against the phrasing of the page being graded.
    Matched on word stems because a runbook writes "rolling back" where the
    dataset writes "rollback".
    """
    root = scenario.get("root_cause") or {}
    keywords = [str(k).lower() for k in (root.get("keywords") or [])]
    if not keywords:
        return 0
    text = f"{heading}\n{section}".lower()
    return sum(1 for k in keywords if k[: max(4, len(k) - 3)] in text)


def _has_stop_condition(section: str) -> bool:
    """A no-action branch needs permission to close *and* a number to compare
    against. "Expected background noise, escalate if well above baseline" is
    neither: it leaves the operator to invent the threshold."""
    lowered = section.lower()
    return any(
        phrase in lowered
        for phrase in (
            "no action",
            "take no action",
            "close the incident",
            "no-action-required",
            "below threshold",
            "sub-threshold",
            "do not remediate",
            "healthy band",
        )
    ) and bool(re.search(r"\d", section))


def _core_metric(query: str) -> str:
    """The first bare metric name inside a PromQL expression."""
    for token in re.findall(r"[a-z_][a-z0-9_]*", query):
        if token not in {
            "sum",
            "rate",
            "min",
            "max",
            "avg",
            "by",
            "le",
            "clamp_min",
            "histogram_quantile",
            "service",
            "job",
        }:
            return token
    return ""


# ── Driver ──────────────────────────────────────────────────────────────────


def load_scenarios() -> List[tuple]:
    out = []
    for split in SPLITS:
        path = DATASET_DIR / f"{split}.json"
        payload = json.loads(path.read_text())
        for scenario in payload["scenarios"]:
            out.append((split, scenario))
    return out


def _normalize_corpus(payload: Any) -> List[Dict[str, Any]]:
    """Accept either flattened runbook records or a raw Notion page dump.

    The page dump keeps Notion's own property names; flatten them the way the
    MCP server's `_page_to_runbook` does so both inputs score identically.
    """
    if isinstance(payload, dict):
        records = payload.get("runbooks") or payload.get("results") or payload.get("pages") or []
    else:
        records = payload

    flattened = []
    for record in records:
        if "properties" not in record:
            flattened.append(record)
            continue
        props = {str(k).lower(): v for k, v in (record.get("properties") or {}).items()}
        flattened.append(
            {
                "runbook_id": record.get("id", ""),
                "title": props.get("name") or props.get("title") or "Untitled",
                "service": props.get("service") or "—",
                "incident_type": props.get("incident type") or props.get("incident_type") or "—",
                "severity": props.get("severity") or "—",
                "status": props.get("status") or "",
                "owner_team": props.get("owner team") or props.get("owner_team") or "",
                "tags": props.get("tags") or "",
                "alert_name": props.get("alert name") or props.get("alert_name") or "",
                "impacted_environment": props.get("environment") or "",
                "content": record.get("content") or "",
                "path": record.get("url") or "notion",
            }
        )
    return flattened


def _overlay_proposed(corpus: List[Dict[str, Any]], proposed_dir: Path) -> List[Dict[str, Any]]:
    """Replace page bodies with locally authored drafts, matched on the H1.

    Lets the same audit grade what is live in Notion and what we are about to
    publish, so the diff is a number rather than a judgement call.
    """
    drafts = {}
    for path in sorted(proposed_dir.glob("*.md")):
        text = path.read_text()
        heading = next(
            (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), ""
        )
        if not heading:
            raise SystemExit(f"{path}: no '# Title' heading to match against the corpus")
        drafts[_normalize(heading)] = text

    matched = set()
    for record in corpus:
        key = _normalize(record.get("title", ""))
        if key in drafts:
            record["content"] = drafts[key]
            matched.add(key)

    unmatched = set(drafts) - matched
    if unmatched:
        raise SystemExit(
            "drafts match no page in the corpus (title drift would orphan the "
            f"rewrite): {sorted(unmatched)}"
        )
    return corpus


def run(corpus_path: Path, proposed_dir: Path | None = None) -> List[Verdict]:
    corpus = _normalize_corpus(json.loads(corpus_path.read_text()))
    if proposed_dir:
        corpus = _overlay_proposed(corpus, proposed_dir)

    verdicts = []
    for split, scenario in load_scenarios():
        alert = scenario["alert"]
        query = _compose_query(
            severity=alert.get("severity", ""),
            service=alert.get("service", ""),
            alert_name=alert.get("alertname", ""),
        )
        hits = search(corpus, query)
        chosen = select_runbook(
            hits, alert_name=alert.get("alertname", ""), service=alert.get("service", "")
        )
        if not chosen:
            verdicts.append(
                Verdict(
                    scenario=scenario["id"],
                    split=split,
                    alert=alert["alertname"],
                    service=alert["service"],
                    runbook="(no hit)",
                    runbook_id="",
                    is_auto_generated=False,
                    allowed=list(scenario.get("allowed_action_types") or []),
                    forbidden=list(scenario.get("forbidden_action_types") or []),
                    notes=["retrieval returned nothing"],
                )
            )
            continue
        # select_runbook returns the search hit; carry the body across.
        body = next(
            (rb for rb in corpus if rb.get("runbook_id") == chosen.get("runbook_id")),
            chosen,
        )
        verdicts.append(grade(scenario, split, dict(chosen, content=body.get("content", ""))))
    return verdicts


def render_table(verdicts: Sequence[Verdict]) -> str:
    lines = []
    width = max(len(v.scenario) for v in verdicts)
    lines.append(f"{'SCENARIO'.ljust(width)}  {'VERDICT':7}  RUNBOOK / GAP")
    lines.append("─" * (width + 60))
    for v in verdicts:
        mark = "PASS" if v.passes else "FAIL"
        lines.append(f"{v.scenario.ljust(width)}  {mark:7}  {v.runbook}" + (f"  → {v.branch}" if v.branch else ""))
        gaps = []
        if v.is_auto_generated:
            gaps.append("auto-generated page")
        if v.missing_prescription:
            gaps.append(f"no step for {', '.join(v.missing_prescription)}")
        if v.missing_prohibition:
            gaps.append(f"does not forbid {', '.join(v.missing_prohibition)}")
        if v.is_no_action and not v.has_stop_condition:
            gaps.append("no numeric do-nothing branch")
        if not v.has_probe_query:
            gaps.append("no recovery-probe query")
        for note in v.notes:
            gaps.append(note)
        for gap in gaps:
            lines.append(f"{' ' * width}           ↳ {gap}")
    passed = sum(1 for v in verdicts if v.passes)
    lines.append("")
    lines.append(f"{passed}/{len(verdicts)} scenarios have a runbook that prescribes the solution.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path, help="JSON dump of runbook records")
    parser.add_argument(
        "--proposed",
        type=Path,
        help="directory of locally authored .md drafts to grade instead of the live bodies",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args()

    verdicts = run(args.corpus, args.proposed)
    if args.format == "json":
        print(json.dumps([v.__dict__ | {"passes": v.passes} for v in verdicts], indent=2))
    else:
        print(render_table(verdicts))
    return 0 if all(v.passes for v in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
