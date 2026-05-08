#!/usr/bin/env python3
"""Conversational narrators for the incident timeline.

Every helper here turns structured incident data into a short, natural
"group chat" message in the voice of the named specialist or the supervisor.
The goal is to replace the rigid `- objective: / - evidence: / - conclusion:`
templates that were previously emitted to the dashboard with text that reads
like a teammate talking to the on-call engineer in Slack.

Design notes:
- Each function is async because it calls an LLM.
- Each function has a deterministic fallback so the timeline never goes blank
  when the LLM call fails or returns nothing useful.
- Roles use their full team title ("Prometheus Specialist", "Loki Specialist",
  "Supervisor") — there are no nicknames.
- Outputs are plain markdown paragraphs; the dashboard renders them with
  `whitespace-pre-wrap` so headings and short bullet lists are fine, but the
  default voice is prose, not key/value pairs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


SPECIALIST_LABELS: Dict[str, str] = {
    "metrics_agent": "Prometheus Specialist",
    "logs_agent": "Loki Specialist",
    "github_agent": "GitHub Specialist",
    "runbooks_agent": "Runbooks Specialist",
}

SPECIALIST_SCOPE: Dict[str, str] = {
    "metrics_agent": "metrics, error rates, latency, traffic, saturation, and golden signals",
    "logs_agent": "application and infrastructure logs, error patterns, and stack traces",
    "github_agent": "recent commits, pull requests, deployments, and rollback candidates",
    "runbooks_agent": "operational runbooks, playbooks, and step-by-step procedures",
}


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, indent=2)
    except Exception:
        return str(value)


def _truncate(text: str, max_length: int = 1800) -> str:
    text = text or ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _alert_to_dict(alert_context: Any) -> Dict[str, Any]:
    if not alert_context:
        return {}
    if isinstance(alert_context, dict):
        return alert_context
    out: Dict[str, Any] = {}
    for attr in ("alert_name", "severity", "service", "cluster", "summary", "description", "labels", "annotations"):
        if hasattr(alert_context, attr):
            out[attr] = getattr(alert_context, attr)
    return out


def _format_alert_block(alert_context: Any) -> str:
    data = _alert_to_dict(alert_context)
    if not data:
        return "No alert payload was attached."

    lines: List[str] = []
    if data.get("alert_name"):
        lines.append(f"alert_name: {data['alert_name']}")
    if data.get("severity"):
        lines.append(f"severity: {data['severity']}")
    if data.get("service"):
        lines.append(f"service: {data['service']}")
    if data.get("cluster"):
        lines.append(f"cluster: {data['cluster']}")
    annotations = data.get("annotations") or {}
    if isinstance(annotations, dict):
        if annotations.get("summary"):
            lines.append(f"summary: {annotations['summary']}")
        if annotations.get("description"):
            lines.append(f"description: {annotations['description']}")
    labels = data.get("labels") or {}
    if isinstance(labels, dict) and labels:
        compact = ", ".join(f"{k}={v}" for k, v in list(labels.items())[:8])
        lines.append(f"labels: {compact}")
    if not lines and data.get("summary"):
        lines.append(f"summary: {data['summary']}")
    return "\n".join(lines) or "No alert payload was attached."


def build_specialist_task_brief(
    *,
    specialist_role: str,
    objective: str,
    alert_context: Any,
    auto_approve: bool = False,
) -> str:
    """Build a rich task brief that the specialist LLM receives as its user prompt.

    Without this, specialists were getting just the alert NAME (no labels,
    no time window, no annotation hints), so they would query Prometheus / Loki
    with hardcoded service/job/instance values that didn't match the alert
    and come back empty — which the supervisor then misinterpreted as
    "monitoring is broken." This helper makes sure every specialist sees:

    * the alert name + summary + description in plain words,
    * the exact label set (so they can plug in correct values),
    * the actionable label hints (reason, error_type, query, endpoint, code, ...),
    * the alert's start time + the recommended query window.
    """
    data = _alert_to_dict(alert_context)
    labels: Dict[str, str] = data.get("labels") or {}
    annotations: Dict[str, str] = data.get("annotations") or {}
    starts_at = (data.get("starts_at") if isinstance(data, dict) else None) or getattr(
        alert_context, "starts_at", None
    )

    # Highest-leverage label hints first — these are the ones we've seen
    # specialists ignore in past audits.
    actionable_keys = (
        "reason", "error_type", "query", "endpoint", "code",
        "service", "job", "instance", "namespace", "pod", "container",
    )
    label_hints = [
        f"{k}={labels[k]}" for k in actionable_keys if labels.get(k)
    ]
    other_labels = [
        f"{k}={v}" for k, v in labels.items() if k not in actionable_keys
    ]

    lines: List[str] = []
    lines.append(f"You are the {specialist_role}.")
    lines.append("")
    lines.append(f"Objective: {objective}")
    lines.append("")
    lines.append("Alert payload (use these exact label values in your tool calls):")
    if data.get("alert_name"):
        lines.append(f"- alert_name: {data['alert_name']}")
    if data.get("severity"):
        lines.append(f"- severity: {data['severity']}")
    if annotations.get("summary"):
        lines.append(f"- summary: {annotations['summary']}")
    if annotations.get("description"):
        lines.append(f"- description: {annotations['description']}")
    if label_hints:
        lines.append(f"- key labels: {', '.join(label_hints)}")
    if other_labels:
        lines.append(f"- other labels: {', '.join(other_labels[:8])}")
    if starts_at:
        lines.append(f"- alert started at: {starts_at}")

    lines.append("")
    lines.append("How to investigate:")
    lines.append(
        "1. Plug the EXACT label values above into your tool calls "
        "(service, job, instance, namespace, pod, endpoint, query, ...). "
        "Do NOT invent labels, do NOT use placeholder names like 'web-service'."
    )
    if starts_at:
        lines.append(
            "2. Query the time window AROUND the alert (5-10 minutes before "
            f"and after {starts_at}). Do not query 'now' — the issue may "
            "have already self-resolved by the time you look."
        )
    else:
        lines.append(
            "2. Query a wide enough window (last 30 minutes by default) so "
            "you don't miss a transient spike that already self-resolved."
        )
    lines.append(
        "3. The alert payload above ALREADY proves that the monitoring "
        "pipeline is working (it produced these numeric values). If your "
        "tool returns empty, that means the symptom has passed or your "
        "label filter is too narrow — NOT that monitoring is broken. "
        "Try a broader query (drop one label at a time) before giving up."
    )
    lines.append(
        "4. Quote any specific label hints (reason, error_type, query, "
        "endpoint, code) when you explain what you found — they are "
        "usually the root-cause signal."
    )
    lines.append(
        "5. Distinguish 'tool returned 5xx / connection error' (a tool "
        "failure — flag it explicitly) from 'tool returned no data' "
        "(a real signal). Never conflate the two."
    )
    if auto_approve:
        lines.append("")
        lines.append(
            "IMPORTANT: produce a complete, actionable response in this "
            "single turn. Do not ask follow-up questions; the on-call "
            "engineer is reading along but not responding."
        )
    return "\n".join(lines)


_NUMERIC_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|ms|s|seconds?|minutes?|requests?|errors?|/s|qps|MB|GB|KB|bytes)?",
    re.IGNORECASE,
)


def _extract_alert_evidence(alert_context: Any) -> List[str]:
    """Pull out the numeric facts the alert ITSELF stated.

    These prove the monitoring pipeline produced data, so the supervisor
    must NOT later conclude that 'monitoring is broken'. We surface them
    explicitly into the synthesis prompt to forbid that bad outcome.
    """
    data = _alert_to_dict(alert_context)
    if not data:
        return []
    blobs: List[str] = []
    annotations = data.get("annotations") or {}
    if isinstance(annotations, dict):
        for key in ("summary", "description"):
            value = annotations.get(key)
            if value:
                blobs.append(str(value))
    labels = data.get("labels") or {}
    if isinstance(labels, dict):
        for key in ("value", "current_value", "threshold"):
            value = labels.get(key)
            if value:
                blobs.append(f"{key}={value}")
    if data.get("summary"):
        blobs.append(str(data["summary"]))

    facts: List[str] = []
    seen: set = set()
    for blob in blobs:
        for match in _NUMERIC_PATTERN.finditer(blob):
            number, unit = match.group(1), match.group(2) or ""
            try:
                if float(number) == 0:
                    continue
            except ValueError:
                continue
            fact = f"{number}{unit}".strip()
            if fact and fact not in seen:
                seen.add(fact)
                facts.append(fact)
            if len(facts) >= 6:
                break
        if len(facts) >= 6:
            break
    return facts


def _format_label_hint_block(alert_context: Any) -> str:
    data = _alert_to_dict(alert_context)
    labels = (data.get("labels") if isinstance(data, dict) else {}) or {}
    if not isinstance(labels, dict):
        return ""
    hints: List[str] = []
    for key in ("reason", "error_type", "query", "endpoint", "code", "service",
                 "job", "instance", "namespace", "pod"):
        if labels.get(key):
            hints.append(f"{key}={labels[key]}")
    return ", ".join(hints)


_TOOL_ERROR_HINTS = (
    "tool failed after",
    "tool returned: error",
    "internal server error",
    "500 server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "connection refused",
    "connection reset",
    "connect timeout",
    "tool unavailable",
    "tool error",
)


def _detect_tool_failures(text: str) -> List[str]:
    """Return short descriptions of tool failures we can spot in raw output."""
    if not text:
        return []
    lower = text.lower()
    hits: List[str] = []
    for marker in _TOOL_ERROR_HINTS:
        if marker in lower:
            hits.append(marker)
    return hits


def _format_findings_block(agent_results: Dict[str, Any]) -> str:
    if not agent_results:
        return "No specialist findings were captured yet."

    blocks: List[str] = []
    for agent_name, response in agent_results.items():
        label = SPECIALIST_LABELS.get(agent_name, agent_name.replace("_", " ").title())
        body_raw = _safe_text(response)
        body = _truncate(body_raw, 1200)
        # Surface any tool failures explicitly so the supervisor narrator
        # treats them as a separate signal from "no data".
        failures = _detect_tool_failures(body_raw)
        header = f"### {label}"
        if failures:
            header += (
                f"\n[TOOL FAILURE DETECTED — markers: {', '.join(sorted(set(failures)))}; "
                "treat this as a tooling bug to flag in 'Next steps', not as the root cause]"
            )
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


async def _invoke_llm(llm: Any, system: str, user: str) -> str:
    try:
        response = await llm.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        content = getattr(response, "content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return _clean_llm_output(str(content or ""))
    except Exception as exc:
        logger.warning("Narrator LLM call failed: %s", exc)
        return ""


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)


def _clean_llm_output(text: str) -> str:
    if not text:
        return ""
    text = _THINK_BLOCK_RE.sub("", text)
    text = _FENCE_RE.sub("", text)
    text = _FENCE_END_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Narrator entry points
# ---------------------------------------------------------------------------


_BASE_SUPERVISOR_TONE = (
    "You are the Supervisor on an SRE incident-response group chat. You speak "
    "to the on-call engineer and a small team of specialists (Prometheus, Loki, "
    "GitHub, Runbooks). Talk like a senior SRE on Slack: short, direct, friendly, "
    "first-person, no key/value bullet templates, no emojis unless they're already "
    "in the alert. Refer to the specialists by their full role name (e.g. "
    "'Prometheus Specialist'). Never invent data or tool output that wasn't given "
    "to you. If something is unknown, say so plainly."
)


async def narrate_supervisor_plan(
    llm: Any,
    *,
    objective: str,
    alert_context: Any,
    visible_queue: Sequence[str],
    reasoning: str = "",
) -> str:
    fallback = _fallback_plan(objective, visible_queue)
    if not llm:
        return fallback

    queue_labels = [SPECIALIST_LABELS.get(a, a) for a in visible_queue]
    queue_text = ", ".join(queue_labels) if queue_labels else "(no specialists yet)"
    alert_block = _format_alert_block(alert_context)

    system = (
        f"{_BASE_SUPERVISOR_TONE}\n\n"
        "Write the very first message of the investigation. In 2-4 short sentences:\n"
        "- Acknowledge the alert in plain language.\n"
        "- Say which specialists you're pulling in and in what order, and why.\n"
        "- End with who you're handing off to first.\n"
        "Do NOT use a bulleted plan or 'objective:' / 'next action:' lines."
    )
    user = (
        f"Incident objective: {objective}\n\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"Specialists I'll engage in order: {queue_text}\n"
        f"My internal reasoning (do NOT quote verbatim): {reasoning or 'standard triage'}\n"
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_plan(objective: str, visible_queue: Sequence[str]) -> str:
    queue_labels = [SPECIALIST_LABELS.get(a, a) for a in visible_queue]
    if not queue_labels:
        return f"Got the page on {objective}. Pulling the team in now to triage."
    if len(queue_labels) == 1:
        return f"Got the page on {objective}. I'll have {queue_labels[0]} take a first look and report back."
    others = ", then ".join(queue_labels[1:])
    return (
        f"Got the page on {objective}. Plan is to start with {queue_labels[0]} "
        f"and then go to {others}. Bringing in {queue_labels[0]} first."
    )


async def narrate_supervisor_handoff(
    llm: Any,
    *,
    next_agent: str,
    objective: str,
    alert_context: Any,
    prior_findings: Dict[str, Any],
    reasoning: str = "",
) -> str:
    fallback = _fallback_handoff(next_agent, objective)
    if not llm:
        return fallback

    label = SPECIALIST_LABELS.get(next_agent, next_agent.replace("_", " ").title())
    scope = SPECIALIST_SCOPE.get(next_agent, "your area")
    alert_block = _format_alert_block(alert_context)
    findings_block = _format_findings_block(prior_findings)

    system = (
        f"{_BASE_SUPERVISOR_TONE}\n\n"
        f"Write a single short Slack-style handoff message addressed to {label}. "
        "1-3 sentences max. Tell them what you want them to look at and why, "
        "referencing anything useful that earlier specialists already reported. "
        "Be specific to the alert (service name, time window, signals). "
        "Do NOT use bullet templates or labelled fields."
    )
    user = (
        f"Incident objective: {objective}\n\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"What earlier specialists found so far:\n{findings_block}\n\n"
        f"Next specialist: {label} (scope: {scope})\n"
        f"Internal reasoning (do not quote verbatim): {reasoning or 'next planned step'}\n"
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_handoff(next_agent: str, objective: str) -> str:
    label = SPECIALIST_LABELS.get(next_agent, next_agent.replace("_", " ").title())
    scope = SPECIALIST_SCOPE.get(next_agent, "your area")
    return f"{label}, can you take this one? Look at {scope} around the {objective} window and let us know what you see."


async def narrate_specialist_finding(
    llm: Any,
    *,
    agent_name: str,
    objective: str,
    alert_context: Any,
    raw_response: str,
) -> str:
    fallback = _fallback_finding(agent_name, raw_response)
    if not llm:
        return fallback

    label = SPECIALIST_LABELS.get(agent_name, agent_name.replace("_", " ").title())
    scope = SPECIALIST_SCOPE.get(agent_name, "your area")
    alert_block = _format_alert_block(alert_context)
    label_hints = _format_label_hint_block(alert_context)
    alert_facts = _extract_alert_evidence(alert_context)
    cleaned_response = _truncate(_safe_text(raw_response), 4000)

    facts_line = ", ".join(alert_facts) if alert_facts else "(none provided)"

    system = (
        f"You are the {label} on an SRE group chat. You just finished checking "
        f"{scope}. Report back to the team in 2-4 short sentences, first person, "
        "Slack tone. Lead with the headline (what you saw or didn't see), then "
        "one sentence of supporting evidence (numbers, query names, log excerpts) "
        "if you have any, then optionally one sentence on what should happen next. "
        "If your tools returned nothing, say so honestly — never invent data.\n\n"
        "HARD RULES:\n"
        "- The alert payload itself contains numeric values (above). That ALREADY "
        "proves the monitoring pipeline produced data. NEVER conclude or imply "
        "that 'monitoring is broken', 'metrics aren't being scraped', or "
        "'the pipeline is misconfigured' just because YOUR follow-up query "
        "returned empty. If your query came back empty, the most likely cause "
        "is a label-filter mismatch or that the spike already self-resolved — "
        "say that, not that monitoring is broken.\n"
        "- Distinguish a TOOL FAILURE (e.g. 5xx, connection error, timeout) "
        "from NO DATA. If a tool returned an error, name the tool and the "
        "error explicitly so the supervisor can flag it.\n"
        "- If the alert's labels contain hints (reason=, error_type=, query=, "
        "endpoint=, code=, ...), reference them by name in your message.\n"
        "- No bullet templates, no labelled fields, no 'objective:'/'evidence:' "
        "lines, no 'Investigation Summary' header."
    )
    user = (
        f"Incident objective: {objective}\n\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"Numeric facts already in the alert (so monitoring DID work): {facts_line}\n"
        f"Actionable label hints in the alert: {label_hints or '(none)'}\n\n"
        f"My raw investigation output (markdown, may include tables and tool results):\n"
        f"---\n{cleaned_response}\n---\n\n"
        "Now write the chat message I should post to the team."
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_finding(agent_name: str, raw_response: str) -> str:
    label = SPECIALIST_LABELS.get(agent_name, agent_name.replace("_", " ").title())
    cleaned = _clean(raw_response)
    if not cleaned:
        return f"{label} here — I didn't get any usable output from my tools on that one."
    snippet = cleaned[:320].rstrip()
    if len(cleaned) > 320:
        snippet += "..."
    return f"{label} here — {snippet}"


async def narrate_supervisor_summary(
    llm: Any,
    *,
    objective: str,
    alert_context: Any,
    agent_results: Dict[str, Any],
) -> str:
    fallback = _fallback_summary(objective, agent_results)
    if not llm:
        return fallback

    alert_block = _format_alert_block(alert_context)
    findings_block = _format_findings_block(agent_results)
    label_hints = _format_label_hint_block(alert_context)
    alert_facts = _extract_alert_evidence(alert_context)
    facts_line = ", ".join(alert_facts) if alert_facts else "(none)"

    system = (
        f"{_BASE_SUPERVISOR_TONE}\n\n"
        "Now you write the wrap-up for the incident. The on-call engineer "
        "wants three things, fast and in plain English:\n"
        "  1. What's happening (one or two sentences)\n"
        "  2. The most likely root cause and WHY it happened, grounded in "
        "     the evidence the specialists gave you AND the alert payload "
        "     itself. If the evidence is thin or empty, SAY THAT — do not "
        "     pretend there's a root cause when there isn't.\n"
        "  3. The next steps to resolve it ASAP — concrete actions, in order.\n\n"
        "Format using exactly these markdown headings, in this order:\n"
        "## TL;DR\n"
        "## What we saw\n"
        "## Most likely root cause\n"
        "## Why it happened\n"
        "## Next steps to resolve\n\n"
        "Each section is a short paragraph or a tight numbered list. Talk like "
        "a teammate, not a report generator. Reference specialists by their full "
        "role name when citing where evidence came from. Never fabricate metric "
        "values, log lines, commits, or service names that aren't in the inputs.\n\n"
        "HARD RULES (these are non-negotiable — past versions of you got this wrong):\n"
        "- The alert payload contains numeric facts (listed below). That PROVES "
        "the monitoring pipeline worked at the time of the incident. You are "
        "FORBIDDEN from concluding that 'monitoring is broken', 'labels don't "
        "match', 'pods are not being scraped', or anything similar. If "
        "specialists' follow-up tool calls came back empty, the realistic "
        "explanations are: (a) the spike has already self-resolved, "
        "(b) the specialist used too narrow a label filter, or "
        "(c) a specific tool call failed (a bug to flag separately). Say "
        "which of these is the case — never blame the pipeline as a whole.\n"
        "- The alert's own label hints (reason=, error_type=, query=, "
        "endpoint=, code=, ...) are usually the strongest root-cause signal. "
        "If they exist, surface them by name in 'Most likely root cause' and "
        "'Why it happened'.\n"
        "- If a specialist reported a tool ERROR (HTTP 500, timeout, connection "
        "refused), call it out under 'What we saw' as a tooling issue and "
        "include 'investigate the tool failure' in 'Next steps' — do NOT let "
        "it contaminate the root-cause analysis."
    )
    user = (
        f"Incident objective: {objective}\n\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"Numeric facts already in the alert (proof the pipeline worked): {facts_line}\n"
        f"Actionable label hints from the alert: {label_hints or '(none)'}\n\n"
        f"Specialist findings (raw):\n{findings_block}\n\n"
        "Now write the wrap-up message."
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_summary(objective: str, agent_results: Dict[str, Any]) -> str:
    if not agent_results:
        return (
            f"## TL;DR\nWe didn't capture any specialist findings for {objective}.\n\n"
            "## Next steps to resolve\n"
            "- Re-run the investigation or check that the data sources (Prometheus, Loki) are reachable."
        )
    lines = [f"## TL;DR\nHere's what the team came back with on {objective}:", ""]
    for agent_name, response in agent_results.items():
        label = SPECIALIST_LABELS.get(agent_name, agent_name.replace("_", " ").title())
        snippet = _clean(_safe_text(response))[:240]
        lines.append(f"- **{label}:** {snippet or 'no usable output.'}")
    lines.extend(
        [
            "",
            "## Next steps to resolve",
            "- Correlate the findings above and decide on a remediation step.",
        ]
    )
    return "\n".join(lines)


async def narrate_followup_answer(
    llm: Any,
    *,
    question: str,
    objective: str,
    alert_context: Any,
    agent_results: Dict[str, Any],
    prior_summary: str,
    incident_status: str = "",
) -> str:
    fallback = _fallback_followup(question, prior_summary)
    if not llm:
        return fallback

    alert_block = _format_alert_block(alert_context)
    findings_block = _format_findings_block(agent_results)
    status_line = f"\nCurrent incident status: {incident_status}\n" if incident_status else ""
    summary_block = _truncate(prior_summary or "(no prior summary captured)", 2400)

    system = (
        f"{_BASE_SUPERVISOR_TONE}\n\n"
        "The investigation has already wrapped up and you're now in a "
        "follow-up Q&A with the on-call engineer in the same incident chat. "
        "Answer their question directly and conversationally, grounded in the "
        "alert context, the specialist findings, and the prior wrap-up summary "
        "below. If they ask 'what are the next steps' or 'give me instructions', "
        "give a concrete, ordered list of actions a human SRE can run right now, "
        "and call out anything risky. Never invent commands, services, or metric "
        "values that aren't in the inputs. If the inputs genuinely don't contain "
        "what they need, say so honestly and suggest the smallest next probe."
    )
    user = (
        f"User's follow-up question: {question}\n\n"
        f"Incident objective: {objective}{status_line}\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"Specialist findings from the original investigation:\n{findings_block}\n\n"
        f"My prior wrap-up summary:\n---\n{summary_block}\n---\n"
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_followup(question: str, prior_summary: str) -> str:
    if prior_summary:
        compact = _truncate(_clean(prior_summary), 600)
        return (
            f"Here's where we landed: {compact}\n\n"
            "If you want me to dig further, point me at metrics, logs, or recent deploys."
        )
    return (
        "I don't have anything fresh on top of what I already shared in this thread. "
        "Want me to re-run a specific check (metrics, logs, recent deploys, or a runbook lookup)?"
    )


async def narrate_chat_greeting(
    llm: Any,
    *,
    user_message: str,
    objective: str,
    alert_context: Any,
    incident_status: str = "",
    prior_summary: str = "",
) -> str:
    fallback = _fallback_greeting(objective, incident_status)
    if not llm:
        return fallback

    alert_block = _format_alert_block(alert_context)
    summary_block = _truncate(prior_summary or "(no prior summary captured)", 1200)
    status_line = f"\nCurrent incident status: {incident_status}\n" if incident_status else ""

    system = (
        f"{_BASE_SUPERVISOR_TONE}\n\n"
        "The user just sent a casual message ('hi', 'hello', 'thanks', etc.) "
        "in the incident chat. Respond like a teammate would: 1-2 short sentences, "
        "warm but not chirpy, that acknowledge them and remind them what this "
        "incident thread is about and what they can ask next. No bullet lists."
    )
    user = (
        f"User message: {user_message}\n\n"
        f"Incident objective: {objective}{status_line}\n"
        f"Alert payload:\n{alert_block}\n\n"
        f"Prior summary (for context only):\n{summary_block}\n"
    )
    out = await _invoke_llm(llm, system, user)
    return out or fallback


def _fallback_greeting(objective: str, incident_status: str) -> str:
    status_part = f" (currently {incident_status})" if incident_status else ""
    return (
        f"Hey — I'm still on the {objective} thread{status_part}. "
        "Want me to walk through what we found, or dig into a specific signal?"
    )
