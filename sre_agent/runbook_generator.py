#!/usr/bin/env python3
"""
Generative runbooks (project #5: generative courses/UI, applied to SRE).

The video's #5 is auto-generating structured learning content (a course) for any
topic. The operational analogue — and the one that actually compounds value here
— is auto-generating a **runbook / postmortem** from every resolved incident and
publishing it into the cluster's Notion runbook database (upserted by title —
see `sre_agent/notion_runbooks.py::upsert_notion_runbook`). That closes a
learning loop: the Planner's runbook RAG (`search_runbooks`, served by
`edge_mcp_servers/mcp_servers/runbooks_notion`) then finds the agent's own
generated guidance the next time that class of incident recurs, so the system
teaches itself — as long as the cluster has a Notion runbook database
configured; a cluster without one simply skips generation (see `write_runbook`).

Generation is deterministic (assembled from the structured data the
investigation already produced) so it always succeeds and is fully testable;
no LLM call is required. The YAML-frontmatter markdown shape is kept because
`generate_runbook_markdown` predates Notion-only hosting and some fields
(the frontmatter dict) are reused directly as Notion page properties — see
`_frontmatter`/`_publish_to_notion`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# kubectl equivalents for the remediation section (mirrors executor.build_command).
_KUBECTL = {
    "restart": "kubectl rollout restart deployment/{target} -n {ns}",
    "scale": "kubectl scale deployment/{target} --replicas=<N> -n {ns}",
    "rollback": "kubectl rollout undo deployment/{target} -n {ns}",
    "patch": "kubectl set resources deployment/{target} -c {target} --limits=<...> -n {ns}",
    "config_change": "kubectl apply -f <rendered-config for {target}> -n {ns}",
    "revert_commit": "gh pr create --title 'Revert <sha>' (revert the bad commit)",
    "escalate": "notify on-call / page (no infra mutation)",
}


@dataclass
class RunbookInput:
    alert_name: str
    service: str
    failure_class: str
    severity: str = "SEV3"
    severity_label: str = "warning"
    hypothesis: str = "See investigation summary."
    confidence: Optional[float] = None
    actions: List[Dict[str, Any]] = field(default_factory=list)  # [{action_type, target}]
    namespace: str = "demo-app"
    verification_status: str = "pending"
    incident_id: Optional[str] = None
    skill_id: Optional[str] = None


def runbook_id(inp: RunbookInput) -> str:
    return f"RB-AUTO-{inp.failure_class}-{inp.service}".replace(" ", "-")


def runbook_filename(inp: RunbookInput) -> str:
    return f"{runbook_id(inp)}.md"


def input_from_act(state: Any, report: Any, skill_id: Optional[str] = None) -> RunbookInput:
    """Build a RunbookInput from graph state + an ActReport."""
    from .skill_store import signature_from_alert  # reuse the same signature logic

    alert = _get(state, "alert_context")
    sig = signature_from_alert(alert)
    labels = _get(alert, "labels", {}) or {}
    severity_label = str(_get(alert, "severity", labels.get("severity", "warning")) or "warning")
    namespace = str(labels.get("namespace", "demo-app"))

    reflector = _get(state, "reflector_analysis")
    hypothesis = str(_get(reflector, "hypothesis", "See investigation summary.") or "See investigation summary.")
    confidence = _get(reflector, "confidence", None)

    # Prefer the actions actually executed; else the proposed action reports.
    executed = _get(report, "executed", None) or []
    proposed = _get(report, "action_reports", None) or []
    actions = executed or proposed

    incident_id = _get(state, "incident_id") or _get(_get(state, "metadata", {}) or {}, "incident_id")

    return RunbookInput(
        alert_name=sig.alert_name,
        service=sig.service,
        failure_class=sig.failure_class,
        severity=str(_get(report, "severity", "SEV3") or "SEV3"),
        severity_label=severity_label,
        hypothesis=hypothesis,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        actions=[{"action_type": _get(a, "action_type", "?"), "target": _get(a, "target", "")} for a in actions],
        namespace=namespace,
        incident_id=str(incident_id) if incident_id else None,
        skill_id=skill_id,
    )


def _frontmatter(inp: RunbookInput) -> Dict[str, Any]:
    outcome = str(inp.verification_status or "pending").lower()
    success = outcome == "resolved"
    tags = sorted(
        {
            inp.service,
            inp.failure_class,
            "auto-generated",
            "incident-response",
            "kubernetes",
            "verified-success" if success else "negative-exemplar",
        }
    )
    return {
        "title": f"{runbook_id(inp)} | {inp.service} | {inp.failure_class.replace('_', ' ').title()}",
        "runbook_id": runbook_id(inp),
        "service": inp.service,
        "incident_type": inp.failure_class,
        "severity": inp.severity,
        "status": "Verified success" if success else "Negative exemplar",
        "review_state": "pending_review",
        "agent_retrievable": True,
        "learning_outcome": "verified_success" if success else outcome,
        "owner_team": "SRE",
        "incident_id": inp.incident_id,
        "skill_id": inp.skill_id,
        "verification_status": inp.verification_status,
        "tags": tags,
        "alert_name": inp.alert_name,
        "impacted_environment": inp.namespace,
        "source_of_truth": "Auto-generated from incident",
        "generated_from_incident": inp.incident_id or "unknown",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "0.1",
    }


def generate_runbook_markdown(inp: RunbookInput) -> str:
    """Assemble a valid, indexable runbook markdown from an incident. Deterministic."""
    import yaml  # lazy; PyYAML is a project dependency

    fm = yaml.safe_dump(_frontmatter(inp), sort_keys=False, default_flow_style=False).strip()
    rid = runbook_id(inp)
    conf = f" (confidence {inp.confidence:.0%})" if inp.confidence is not None else ""

    steps = []
    for i, a in enumerate(inp.actions, 1):
        at = str(a.get("action_type", "?")).lower()
        tmpl = _KUBECTL.get(at, "# (no command mapping)")
        cmd = tmpl.format(target=a.get("target", "<target>"), ns=inp.namespace)
        steps.append(f"{i}. **{at}** `{a.get('target', '')}`\n   ```bash\n   {cmd}\n   ```")
    steps_md = "\n".join(steps) if steps else "1. No automated remediation recorded; investigate manually."

    skill_line = f"\nThis remediation is stored as reusable skill `{inp.skill_id}`.\n" if inp.skill_id else ""

    return f"""---
{fm}
---

# {rid} | {inp.service} | {inp.failure_class.replace('_', ' ').title()}

> Auto-generated from incident `{inp.incident_id or 'unknown'}`. Review and promote
> to an owned runbook if the guidance holds.

## Summary

Use this runbook when **{inp.alert_name}** fires on **{inp.service}**
(failure class: *{inp.failure_class}*, severity {inp.severity}).

## Symptoms

- Alert `{inp.alert_name}` firing (severity: {inp.severity_label}).
- Impacted service: `{inp.service}` in namespace `{inp.namespace}`.

## Root cause (hypothesis)

{inp.hypothesis}{conf}

## Remediation (what the agent applied)

{steps_md}
{skill_line}
## Verification

- Re-check the Golden Signals for `{inp.service}` after remediation.
- Confirm the alert `{inp.alert_name}` clears and error rate / latency return to baseline.
- Current verification status at generation time: **{inp.verification_status}**.

## Prevention / next steps

- Add or tune an SLO alert for this failure class if missing.
- If this recurs, the agent will propose the stored skill automatically.
- Promote this auto-runbook to an owned, reviewed runbook.
"""


async def generate_runbook_llm(inp: RunbookInput, llm: Any) -> str:
    """LLM-authored runbook body (genuinely generative) with deterministic fallback.

    The frontmatter stays deterministic (so the corpus stays machine-indexable),
    but the prose is written by the model from the incident facts — richer and
    more useful than a fixed template. Falls back to the template if the LLM
    call fails, so generation never breaks the pipeline.
    """
    import yaml
    from langchain_core.messages import HumanMessage, SystemMessage

    commands = "\n".join(
        f"- {a.get('action_type')} {a.get('target')}" for a in inp.actions
    ) or "- (none recorded)"
    facts = (
        f"alert={inp.alert_name} service={inp.service} failure_class={inp.failure_class} "
        f"severity={inp.severity}\nhypothesis={inp.hypothesis}\napplied_actions:\n{commands}"
    )
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=(
                "You write concise SRE runbooks. Given incident facts, produce a markdown "
                "body with these sections: Summary, Symptoms, Root cause, Remediation "
                "(include the applied actions as concrete steps), Verification, Prevention. "
                "Do NOT include YAML frontmatter or a top-level title; start at '## Summary'."
            )),
            HumanMessage(content=facts),
        ])
        body = str(getattr(resp, "content", resp)).strip()
        if "## " not in body:
            raise ValueError("LLM body missing sections")
    except Exception as e:
        logger.warning(f"RunbookGenerator: LLM generation failed ({e}); using template.")
        return generate_runbook_markdown(inp)

    fm = yaml.safe_dump(_frontmatter(inp), sort_keys=False, default_flow_style=False).strip()
    rid = runbook_id(inp)
    return (
        f"---\n{fm}\n---\n\n# {rid} | {inp.service} | "
        f"{inp.failure_class.replace('_', ' ').title()}\n\n"
        f"> Auto-generated (LLM) from incident `{inp.incident_id or 'unknown'}`.\n\n{body}\n"
    )


def _notion_creds(execution_context: Any) -> tuple[Optional[str], Optional[str]]:
    if execution_context is None:
        return None, None
    creds = getattr(execution_context, "credentials", {}) or {}
    return creds.get("notion_api_key"), creds.get("notion_database_id")


async def _publish_to_notion(inp: RunbookInput, markdown: str, execution_context: Any) -> Optional[str]:
    """Upsert the generated runbook into this cluster's Notion database.

    Returns the Notion page URL, or ``None`` when the cluster has no Notion
    runbook database configured — generation is skipped, not an error, since
    there is no local corpus left to fall back to.
    """
    api_key, database_id = _notion_creds(execution_context)
    if not (api_key and database_id):
        logger.info("RunbookGenerator: no Notion runbook database configured for this cluster; skipping")
        return None

    from .notion_runbooks import upsert_notion_runbook

    fm = _frontmatter(inp)
    page = await upsert_notion_runbook(
        api_key,
        database_id,
        title=str(fm["title"]),
        markdown_body=markdown,
        service=inp.service,
        incident_type=inp.failure_class,
        severity=inp.severity,
    )
    return page.get("path") or page.get("id")


async def write_runbook(inp: RunbookInput, execution_context: Any = None) -> Optional[str]:
    """Generate (deterministic) and upsert the runbook into Notion; returns the page URL."""
    published = await _publish_to_notion(inp, generate_runbook_markdown(inp), execution_context)
    if published:
        logger.info(f"📝 RunbookGenerator: wrote {runbook_id(inp)} -> {published}")
    return published


async def write_runbook_generative(inp: RunbookInput, llm: Any, execution_context: Any = None) -> Optional[str]:
    """LLM-author the runbook (deterministic fallback inside) and upsert it into Notion."""
    published = await _publish_to_notion(inp, await generate_runbook_llm(inp, llm), execution_context)
    if published:
        logger.info(f"📝 RunbookGenerator: wrote {runbook_id(inp)} -> {published} (generative)")
    return published
