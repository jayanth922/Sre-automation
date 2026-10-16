"""Runbooks are the only source of remediations.

The agent diagnoses; it does not invent fixes. A plan may change the cluster
only with actions a human-authored runbook prescribes for the firing alert, and
only with actions one runbook branch prescribes together -- mixing the restart
from one branch with the scale from another is a combination nobody wrote
down. When no runbook covers the plan, the plan is replaced by an escalation
that tells the operator automated remediation is not possible, rather than
letting the model problem-solve its way to a mutation.

This is a deterministic check over runbook text, not a prompt instruction: the
planner can be told to follow runbooks, but only code can stop it when it
doesn't. Three things must hold:

- The runbook addresses the alert: its ``Alert Name:`` line (or the Notion
  ``alert_name`` property) lists the firing alert. Search scoring is loose --
  one shared token ranks a page -- so retrieval alone proves nothing, and a
  ``name="<service>"`` placeholder in an unrelated runbook must not license a
  restart of whatever service happens to be alerting.
- A section whose heading contains "Action" names the executor tool call the
  action resolves to, e.g. ``restart_deployment(name="payment-service",
  namespace="meridian")``. The tool is resolved with the executor's own
  ``live_tool_for_action``, so an env-var prescription never licenses a
  memory-limit patch. Calls are matched in plain text because Notion's
  markdown export drops the inline-code backticks the source runbooks use, and
  a call on a negated line ("do not ``scale_deployment(...)``") prescribes
  nothing.
- The runbook is not a draft. Runbooks Sentinel wrote itself carry an
  ``Auto-generated`` banner (and an ``RB-AUTO`` title): they authorize nothing
  until a human reviews one and removes the banner. Otherwise a verified AI fix
  would become a runbook and then a "runbook-sanctioned" fix, which is the AI
  problem-solving this module exists to keep out.

Read-only ``inspect`` and ``escalate`` need no prescription: looking is never a
fix, and escalating is the answer when no runbook applies.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, List, Optional, Sequence, Tuple

# The code-change actions dispatch through github_exec rather than the
# executor's infra map; everything else resolves through live_tool_for_action.
_CODE_CHANGE_TOOLS = {
    "revert_commit": "create_revert_pr",
    "revert_pr": "create_revert_pr",
    "code_fix": "create_fix_pr",
}
PRESCRIBABLE_TOOLS: FrozenSet[str] = frozenset({
    "restart_deployment",
    "rollback_deployment",
    "scale_deployment",
    "recreate_pod",
    "patch_deployment_env",
    "patch_resource_limits",
    "create_revert_pr",
    "create_fix_pr",
})
NON_MUTATING_ACTIONS: FrozenSet[str] = frozenset({"inspect", "escalate"})

_CALL = re.compile(r"\b(" + "|".join(sorted(PRESCRIBABLE_TOOLS)) + r")\(([^)]*)\)")
_NAME_ARG = re.compile(r"""\bname\s*=\s*["']([^"']+)["']""")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_ACTION_HEADING = re.compile(r"\baction\b", re.IGNORECASE)
_NEGATED = re.compile(r"\b(do not|don't|never|must not)\b", re.IGNORECASE)
_DRAFT_BANNER = re.compile(r"^\s*>\s*auto-generated\b", re.IGNORECASE | re.MULTILINE)
_DRAFT_TITLE = re.compile(r"^\s*RB-AUTO\b", re.IGNORECASE)
_ALERT_LINE = re.compile(r"^[\s*_>-]*alert names?[\s*_]*:[\s*_]*(.+)$", re.IGNORECASE | re.MULTILINE)
_ALERT_SPLIT = re.compile(r"[,;/|]|\s+or\s+|\s+and\s+")

WILDCARD = "*"


def runbook_only_remediation() -> bool:
    """On unless the operator explicitly restores model-authored plans."""
    return os.getenv("RUNBOOK_ONLY_REMEDIATION", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )


@dataclass(frozen=True)
class Prescription:
    tool: str
    target: str  # deployment name, or WILDCARD for a `<service>` placeholder


@dataclass(frozen=True)
class RunbookBranch:
    heading: str
    prescriptions: Tuple[Prescription, ...]

    def covers(self, tool: str, target: str) -> bool:
        return any(
            p.tool == tool and p.target in (WILDCARD, target) for p in self.prescriptions
        )


@dataclass(frozen=True)
class Runbook:
    title: str
    url: Optional[str]
    branches: Tuple[RunbookBranch, ...]
    alert_names: FrozenSet[str] = frozenset()
    draft: bool = False

    def addresses(self, alert_name: str) -> bool:
        return _normalize_alert(alert_name) in self.alert_names


@dataclass(frozen=True)
class RunbookVerdict:
    authorized: bool
    runbook: Optional[str] = None
    url: Optional[str] = None
    branch: Optional[str] = None
    unprescribed: Tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def reference(self) -> Optional[str]:
        if not self.runbook:
            return None
        return f"{self.runbook} → {self.branch}" if self.branch else self.runbook


def _normalize_alert(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _normalize_target(target: str) -> str:
    """`deployment/payment-service`, `meridian/payment-service` → `payment-service`."""
    bare = str(target or "").strip().strip("`'\"").rsplit("/", 1)[-1]
    return bare.strip().lower()


def action_tool(action: Any) -> Optional[str]:
    """The tool that would carry out ``action``, or None if nothing can."""
    action_type = str(getattr(action, "action_type", "")).lower()
    if action_type in _CODE_CHANGE_TOOLS:
        return _CODE_CHANGE_TOOLS[action_type]
    from .executor import live_tool_for_action

    return live_tool_for_action(action)


def is_draft(title: str, markdown: str) -> bool:
    return bool(_DRAFT_TITLE.search(title or "") or _DRAFT_BANNER.search(markdown or ""))


def _alert_names(markdown: str, extra: Iterable[str]) -> FrozenSet[str]:
    raw: List[str] = list(extra)
    raw.extend(m.group(1) for m in _ALERT_LINE.finditer(markdown or ""))
    names = set()
    for chunk in raw:
        for part in _ALERT_SPLIT.split(str(chunk or "")):
            normalized = _normalize_alert(part)
            if normalized:
                names.add(normalized)
    return frozenset(names)


def _prescriptions(lines: Iterable[str]) -> Tuple[Prescription, ...]:
    found: List[Prescription] = []
    for line in lines:
        if _NEGATED.search(line):
            continue
        for tool, args in _CALL.findall(line):
            name = _NAME_ARG.search(args)
            raw = name.group(1).strip() if name else ""
            target = WILDCARD if not raw or raw.startswith("<") else _normalize_target(raw)
            found.append(Prescription(tool=tool, target=target))
    return tuple(found)


def parse_runbook(
    title: str,
    markdown: str,
    url: Optional[str] = None,
    alert_names: Iterable[str] = (),
) -> Runbook:
    """Split a runbook into its Action sections and what each prescribes."""
    branches: List[RunbookBranch] = []
    heading: Optional[str] = None
    body: List[str] = []
    in_fence = False

    def close() -> None:
        if heading is not None and _ACTION_HEADING.search(heading):
            branches.append(RunbookBranch(heading, _prescriptions(body)))

    for line in (markdown or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            close()
            heading, body = match.group(1).strip(), []
        else:
            body.append(line)
    close()
    return Runbook(
        title=title,
        url=url,
        branches=tuple(branches),
        alert_names=_alert_names(markdown, alert_names),
        draft=is_draft(title, markdown),
    )


def applicable_runbooks(runbooks: Sequence[Runbook], alert_name: str) -> List[Runbook]:
    """Reviewed runbooks that list this alert -- the only ones that may authorize."""
    return [rb for rb in runbooks if not rb.draft and rb.addresses(alert_name)]


def no_runbook_verdict(runbooks: Sequence[Runbook], alert_name: str) -> RunbookVerdict:
    reason = f"No runbook addresses {alert_name}."
    drafts = [rb.title for rb in runbooks if rb.draft and rb.addresses(alert_name)]
    if drafts:
        reason += (
            f" Only unreviewed auto-generated drafts match ({', '.join(drafts)});"
            " a human must review one and remove its Auto-generated banner before"
            " it can authorize a fix."
        )
    return RunbookVerdict(authorized=False, reason=reason)


def authorize_plan(
    actions: Sequence[Any], runbooks: Sequence[Runbook], alert_name: str
) -> RunbookVerdict:
    """Is every mutating action prescribed by one branch of one runbook for this alert?"""
    applicable = applicable_runbooks(runbooks, alert_name)
    mutating = [
        (
            action_tool(a),
            _normalize_target(getattr(a, "target", "")),
            f"{str(getattr(a, 'action_type', '')).lower()} {getattr(a, 'target', '')}",
        )
        for a in actions
        if str(getattr(a, "action_type", "")).lower() not in NON_MUTATING_ACTIONS
    ]
    if not applicable:
        verdict = no_runbook_verdict(runbooks, alert_name)
        return RunbookVerdict(
            authorized=False,
            unprescribed=tuple(label for _, _, label in mutating),
            reason=verdict.reason,
        )
    if not mutating:
        rb = applicable[0]
        return RunbookVerdict(
            authorized=True, runbook=rb.title, url=rb.url,
            reason="The plan changes nothing, so no prescription is needed.",
        )
    for rb in applicable:
        for branch in rb.branches:
            if all(tool and branch.covers(tool, target) for tool, target, _ in mutating):
                return RunbookVerdict(
                    authorized=True, runbook=rb.title, url=rb.url, branch=branch.heading,
                    reason=f"Every action is prescribed by '{rb.title}' → '{branch.heading}'.",
                )
    unprescribed = tuple(
        label
        for tool, target, label in mutating
        if not tool
        or not any(b.covers(tool, target) for rb in applicable for b in rb.branches)
    )
    titles = ", ".join(rb.title for rb in applicable)
    if unprescribed:
        reason = f"No runbook for {alert_name} prescribes {', '.join(unprescribed)} (checked: {titles})."
    else:
        reason = (
            "Each action appears in a runbook, but no single runbook branch "
            f"prescribes them together (checked: {titles})."
        )
    return RunbookVerdict(authorized=False, unprescribed=unprescribed, reason=reason)


def runbook_gap_message(verdict: RunbookVerdict) -> str:
    """What the operator is told when the runbooks do not cover the fix."""
    return (
        f"{verdict.reason} Automated remediation is not possible: Sentinel only "
        "carries out fixes a runbook prescribes. The diagnosis has been escalated "
        "for a human to resolve; write or update a runbook to automate this next time."
    )


def _tool_text(result: Any) -> str:
    """MCP tools return a JSON string or a list of text content blocks."""
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        return "".join(
            (item.get("text", "") if isinstance(item, dict) else str(getattr(item, "text", item)))
            for item in result
        )
    return str(result or "")


def _tool_json(result: Any) -> Any:
    import json

    try:
        return json.loads(_tool_text(result))
    except (TypeError, ValueError):
        return None


async def _call(tool: Any, args: dict) -> Any:
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(args)
    return tool.invoke(args)


async def retrieve_runbooks(
    search_tool: Any,
    content_tool: Any,
    alert_name: str,
    service: str = "",
    limit: int = 5,
) -> List[Tuple[Runbook, str]]:
    """Search for the alert's runbooks and parse each hit's full text.

    Search returns excerpts, and an excerpt can cut a branch's tool call off;
    authorizing from it would refuse a prescribed fix, or worse, read a
    negated call without its "do not". Every hit is re-read in full.
    """
    if search_tool is None or content_tool is None or not alert_name:
        return []
    args = {"query": alert_name, "alert_name": alert_name}
    if service:
        args["service"] = service
    found = _tool_json(await _call(search_tool, args))
    hits = found.get("results", []) if isinstance(found, dict) else []
    runbooks: List[Tuple[Runbook, str]] = []
    for hit in hits[:limit]:
        page_id = str((hit or {}).get("runbook_id") or "").strip()
        if not page_id:
            continue
        page = _tool_json(await _call(content_tool, {"page_id": page_id}))
        if not isinstance(page, dict) or page.get("error") or not page.get("content"):
            continue
        title = str(page.get("title") or hit.get("title") or page_id)
        markdown = str(page["content"])
        runbooks.append((
            parse_runbook(
                title,
                markdown,
                url=page.get("path") or hit.get("path"),
                alert_names=[str(page.get("alert_name") or hit.get("alert_name") or "")],
            ),
            markdown,
        ))
    return runbooks
