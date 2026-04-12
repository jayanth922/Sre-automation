#!/usr/bin/env python3

import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend import crud, database

logger = logging.getLogger(__name__)

VISIBLE_SPECIALIST_LABELS = {
    "metrics_agent": "Prometheus Specialist",
    "logs_agent": "Loki Specialist",
    "github_agent": "GitHub Specialist",
    "runbooks_agent": "Runbooks Specialist",
}

VISIBLE_SPECIALIST_ROLES = {
    "metrics_agent": "prometheus_specialist",
    "logs_agent": "loki_specialist",
    "github_agent": "github_specialist",
    "runbooks_agent": "runbooks_specialist",
}

VISIBLE_SPECIALIST_ORDER = [
    "metrics_agent",
    "logs_agent",
    "github_agent",
    "runbooks_agent",
]


def internal_agent_name(agent_type: str) -> str:
    mapping = {
        "metrics": "metrics_agent",
        "logs": "logs_agent",
        "github": "github_agent",
        "runbooks": "runbooks_agent",
        "kubernetes": "kubernetes_agent",
    }
    return mapping.get(agent_type, agent_type)


def visible_specialist_label(agent_name: str) -> str:
    return VISIBLE_SPECIALIST_LABELS.get(agent_name, agent_name.replace("_", " ").title())


def visible_specialist_role(agent_name: str) -> str:
    return VISIBLE_SPECIALIST_ROLES.get(agent_name, "system")


def filter_visible_specialists(agent_names: Sequence[str]) -> List[str]:
    filtered: List[str] = []
    seen = set()
    for agent_name in agent_names:
        if agent_name in VISIBLE_SPECIALIST_ROLES and agent_name not in seen:
            filtered.append(agent_name)
            seen.add(agent_name)
    return filtered


def infer_visible_specialist_queue(query: str, plan_agents: Sequence[str]) -> List[str]:
    queue = filter_visible_specialists(plan_agents)
    if queue:
        return queue

    normalized_query = re.sub(r"\s+", " ", query.lower())
    heuristics: List[Tuple[Iterable[str], str]] = [
        (("metric", "metrics", "latency", "traffic", "availability", "p95", "prometheus"), "metrics_agent"),
        (("log", "logs", "error", "trace", "exception", "loki"), "logs_agent"),
        (("git", "github", "commit", "pull request", "pr", "deploy", "release", "rollback"), "github_agent"),
        (("runbook", "playbook", "procedure", "escalation", "troubleshoot"), "runbooks_agent"),
    ]

    for keywords, agent_name in heuristics:
        if any(keyword in normalized_query for keyword in keywords):
            queue.append(agent_name)

    if not queue:
        queue = ["metrics_agent", "logs_agent"]

    return filter_visible_specialists(queue)


def _truncate(text: str, max_length: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."


def _clean_public_query(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return "the incident"

    cleaned = re.sub(r"^as the [^,]+,?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^investigate alert:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^investigate:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^follow-up question:?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .:")
    return cleaned or "the incident"


def _is_low_information_response(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower().strip(" .!?:"))
    if not normalized:
        return True

    low_information_markers = {
        "okay",
        "ok",
        "done",
        "noted",
        "yes",
        "no",
        "n/a",
        "na",
        "none",
        "unknown",
        "unclear",
        "still investigating",
        "working on it",
        "no data",
        "no data available",
        "unable to tell",
        "i don't know",
        "i do not know",
    }
    return normalized in low_information_markers or len(normalized.split()) <= 2


def _clean_response_lines(response: str) -> List[str]:
    lines: List[str] = []
    for raw_line in response.splitlines():
        cleaned = raw_line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^[#>*\-\u2022\d.\)\s]+", "", cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _extract_evidence_text(response: str) -> str:
    if _is_low_information_response(response):
        return "No concrete evidence was provided."

    lines = _clean_response_lines(response)
    if not lines:
        return "No concrete evidence was provided."

    evidence_candidates: List[str] = []
    for line in lines:
        lower_line = line.lower()
        if any(marker in lower_line for marker in ["tool", "action", "thinking", "retrieve memory", "call"]):
            continue
        if any(marker in lower_line for marker in ["error", "latency", "cpu", "memory", "timeout", "log", "commit", "deploy", "alert", "%"]):
            evidence_candidates.append(line)

    if not evidence_candidates:
        evidence_candidates = lines[:2]

    return _truncate(" | ".join(evidence_candidates[:2]), 240)


def _extract_conclusion_text(response: str) -> str:
    if _is_low_information_response(response):
        return "The specialist did not provide a concrete conclusion."

    lines = _clean_response_lines(response)
    if not lines:
        return "The specialist did not provide a concrete conclusion."

    joined = " ".join(lines)
    sentence_candidates = re.split(r"(?<=[.!?])\s+", joined)
    for candidate in sentence_candidates:
        lowered = candidate.strip().lower()
        if not lowered:
            continue
        if lowered.startswith(("i checked", "i reviewed", "i looked", "i queried", "tool output", "according to")):
            continue
        return _truncate(candidate.strip(), 180)

    return _truncate(sentence_candidates[0].strip() if sentence_candidates else joined, 180)


def _infer_confidence_from_response(response: str) -> str:
    lower_response = response.lower()
    if _is_low_information_response(response):
        return "low"
    if any(token in lower_response for token in ["high confidence", "very likely", "strong evidence", "clear evidence", "confirmed"]):
        return "high"
    if any(token in lower_response for token in ["uncertain", "maybe", "might", "appears", "suggests", "likely", "no data", "unable"]):
        return "medium"
    if any(char.isdigit() for char in response):
        return "medium"
    return "medium"


def _normalize_specialist_finding(agent_name: str, current_query: str, response: str) -> Dict[str, str]:
    visible_label = visible_specialist_label(agent_name)
    objective = _truncate(_clean_public_query(current_query or f"Investigate {visible_label}"), 180)
    evidence = _extract_evidence_text(response)
    conclusion = _extract_conclusion_text(response)
    confidence = _infer_confidence_from_response(response)
    next_step = _infer_next_step(agent_name)

    return {
        "visible_label": visible_label,
        "objective": objective,
        "evidence": evidence,
        "conclusion": conclusion,
        "confidence": confidence,
        "recommended_next_step": next_step,
    }


def _extract_numeric_fact_mentions(text: str) -> Dict[str, List[str]]:
    if not text:
        return {}

    patterns = {
        "error rate": [
            r"(?:error rate|errors?)\D{0,24}(\d+(?:\.\d+)?%)",
            r"(\d+(?:\.\d+)?%)\D{0,24}(?:error rate|errors?)",
        ],
        "latency": [
            r"(?:latency|response time|p95|p99)\D{0,24}(\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|mins|minutes))",
            r"(\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|mins|minutes))\D{0,24}(?:latency|response time|p95|p99)",
        ],
        "cpu": [
            r"(?:cpu(?: usage| utilization)?|cpu)\D{0,24}(\d+(?:\.\d+)?%)",
            r"(\d+(?:\.\d+)?%)\D{0,24}(?:cpu(?: usage| utilization)?|cpu)",
        ],
        "memory": [
            r"(?:memory(?: usage| utilization)?|memory|mem)\D{0,24}(\d+(?:\.\d+)?%)",
            r"(\d+(?:\.\d+)?%)\D{0,24}(?:memory(?: usage| utilization)?|memory|mem)",
        ],
        "time window": [
            r"(?:last|past|during|over|within|from|between)\D{0,24}(\d+(?:\.\d+)?\s*(?:seconds?|minutes?|hours?|days?|s|m|h)|\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?(?:\s*(?:utc|z))?)",
        ],
    }

    fact_values: Dict[str, List[str]] = {}
    for label, label_patterns in patterns.items():
        values = []
        for pattern in label_patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = match.group(1) if match.groups() else match.group(0)
                normalized = re.sub(r"\s+", " ", value.strip().lower().strip(" ,.;:"))
                if normalized and normalized not in values:
                    values.append(normalized)
        if values:
            fact_values[label] = values

    return fact_values


def _detect_conflicting_numeric_facts(texts: Sequence[str]) -> Dict[str, List[str]]:
    combined: Dict[str, List[str]] = {}
    for text in texts:
        for label, values in _extract_numeric_fact_mentions(text).items():
            existing = combined.setdefault(label, [])
            for value in values:
                if value not in existing:
                    existing.append(value)

    return {label: values for label, values in combined.items() if len(values) > 1}


def _alert_context_to_text(alert_context: Any) -> str:
    if not alert_context:
        return ""

    if isinstance(alert_context, dict):
        alert_name = alert_context.get("alert_name", "")
        severity = alert_context.get("severity", "")
        annotations = alert_context.get("annotations", {}) or {}
    else:
        alert_name = getattr(alert_context, "alert_name", "")
        severity = getattr(alert_context, "severity", "")
        annotations = getattr(alert_context, "annotations", {}) or {}

    annotation_summary = annotations.get("summary", "") if isinstance(annotations, dict) else ""
    annotation_description = annotations.get("description", "") if isinstance(annotations, dict) else ""

    parts = [part for part in [alert_name, severity, annotation_summary, annotation_description] if part]
    return " ".join(parts)


def _first_non_empty_sentence(text: str) -> str:
    stripped = re.sub(r"\s+", " ", text.strip())
    if not stripped:
        return "No visible response captured."
    sentence = re.split(r"(?<=[.!?])\s+", stripped)[0]
    return _truncate(sentence, 180)


def _pick_evidence_lines(response: str) -> str:
    return _extract_evidence_text(response)


def _infer_confidence(response: str) -> str:
    return _infer_confidence_from_response(response)


def _infer_next_step(agent_name: str) -> str:
    mapping = {
        "metrics_agent": "Correlate with logs and code changes.",
        "logs_agent": "Correlate with metrics and code changes.",
        "github_agent": "Check whether the suspect change aligns with the symptom window.",
        "runbooks_agent": "Validate the best runbook path and any safe next actions.",
    }
    return mapping.get(agent_name, "Supervisor should correlate this finding with the rest of the thread.")


def build_supervisor_plan_content(query: str, plan: Dict[str, Any], visible_queue: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
    visible_labels = [visible_specialist_label(agent_name) for agent_name in visible_queue]
    clean_objective = _clean_public_query(query)
    step_text_by_agent = {
        "metrics_agent": "Check the metrics around the alert window and confirm the scope of the impact.",
        "logs_agent": "Review logs for matching error patterns, timeouts, or repeated failures.",
        "github_agent": "Correlate the incident with recent deployments, commits, or pull requests.",
        "runbooks_agent": "Check the safest runbook path and confirm the next operational step.",
    }
    content_lines = [
        "Investigation plan",
        f"- objective: {clean_objective}",
        f"- selected specialists: {', '.join(visible_labels) if visible_labels else 'none yet'}",
    ]
    if visible_queue:
        content_lines.append("- investigation steps:")
        for agent_name in visible_queue:
            content_lines.append(
                f"  - {visible_specialist_label(agent_name)}: {step_text_by_agent.get(agent_name, 'Review the incident evidence and report back clearly.')}"
            )
    if visible_labels:
        content_lines.append(f"- next action: start with {visible_labels[0]}")

    payload = {
        "query": query,
        "plan": plan,
        "visible_queue": list(visible_queue),
    }
    return "\n".join(content_lines), payload


def build_supervisor_decision_content(next_agent: str, reasoning: str, remaining_agents: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
    visible_label = visible_specialist_label(next_agent) if next_agent != "aggregate" else "Supervisor summary"
    remaining_labels = [visible_specialist_label(agent_name) for agent_name in remaining_agents]
    content_lines = [
        "Routing update",
        f"- next specialist: {visible_label}" if next_agent != "aggregate" else "- next action: synthesize the findings",
        f"- why: {reasoning}",
    ]
    if remaining_labels:
        content_lines.append(f"- remaining specialists: {', '.join(remaining_labels)}")

    payload = {
        "next_agent": next_agent,
        "reasoning": reasoning,
        "remaining_agents": list(remaining_agents),
    }
    return "\n".join(content_lines), payload


def build_specialist_finding_content(agent_name: str, current_query: str, response: str) -> Tuple[str, Dict[str, Any]]:
    normalized = _normalize_specialist_finding(agent_name, current_query, response)

    content_lines = [
        f"{normalized['visible_label']} finding",
        f"- objective: {normalized['objective']}",
        f"- evidence: {normalized['evidence']}",
        f"- conclusion: {normalized['conclusion']}",
        f"- confidence: {normalized['confidence']}",
        f"- recommended next step: {normalized['recommended_next_step']}",
    ]

    payload = {
        "agent_name": agent_name,
        "speaker_role": visible_specialist_role(agent_name),
        **normalized,
    }
    return "\n".join(content_lines), payload


def build_supervisor_summary_content(
    final_response: str,
    agent_results: Dict[str, Any],
    query: str = "",
    alert_context: Any = None,
) -> Tuple[str, Dict[str, Any]]:
    normalized_findings: List[Dict[str, Any]] = []
    for agent_name, response in agent_results.items():
        if not response:
            continue
        normalized_findings.append(_normalize_specialist_finding(agent_name, query, str(response)))

    if not query and not alert_context:
        content = final_response.strip() or "Investigation summary is available."
        payload = {
            "source": "supervisor.aggregate_responses",
            "specialists_invoked": list(agent_results.keys()),
            "objective": "the incident",
            "alert_context": None,
            "normalized_findings": normalized_findings,
            "conflicting_numeric_facts": {},
        }
        return content, payload

    objective = _truncate(_clean_public_query(query), 180) if query else "the incident"
    alert_text = _alert_context_to_text(alert_context)

    summary_lines = [
        "## Incident Summary",
        "",
        f"**Objective:** {objective}",
    ]

    if alert_text:
        summary_lines.extend(["", f"**Alert context:** {_truncate(alert_text, 220)}"])

    if normalized_findings:
        summary_lines.extend(["", "**Visible specialist findings:**"])
        for finding in normalized_findings:
            summary_lines.append(
                f"- **{finding['visible_label']}**: {finding['conclusion']}"
            )
            summary_lines.append(f"  - Evidence: {finding['evidence']}")
            summary_lines.append(f"  - Confidence: {finding['confidence']}")
            summary_lines.append(f"  - Recommended next step: {finding['recommended_next_step']}")
    else:
        summary_lines.extend(["", "**Visible specialist findings:** No specialist findings were captured."])

    conflict_sources: List[str] = []
    if alert_text:
        conflict_sources.append(alert_text)
    conflict_sources.extend(
        [
            " ".join([finding["evidence"], finding["conclusion"]])
            for finding in normalized_findings
        ]
    )
    conflicts = _detect_conflicting_numeric_facts(conflict_sources)

    if conflicts:
        conflict_lines = ", ".join(f"{label}: {', '.join(values)}" for label, values in conflicts.items())
        summary_lines.extend(
            [
                "",
                "**Conclusion:** The available facts are inconsistent, so no single settled value should be treated as confirmed yet.",
                f"**Conflicts:** {conflict_lines}.",
                "**Next step:** Reconcile the conflicting source data before closing the incident.",
            ]
        )
    else:
        summary_lines.extend(
            [
                "",
                "**Conclusion:** The visible evidence is still limited, so the incident remains unresolved.",
                "**Next step:** Continue with the remaining checks until the evidence is consistent.",
            ]
        )

    grounded_summary = "\n".join(summary_lines).strip()
    content = grounded_summary or final_response.strip() or "Investigation summary is available."
    payload = {
        "source": "supervisor.aggregate_responses",
        "specialists_invoked": list(agent_results.keys()),
        "objective": objective,
        "alert_context": alert_text or None,
        "normalized_findings": normalized_findings,
        "conflicting_numeric_facts": conflicts,
    }
    return content, payload


def build_supervisor_direct_answer_content(question: str, answer: str, basis: str) -> Tuple[str, Dict[str, Any]]:
    content_lines = [answer.strip() or "I can answer that directly."]
    payload = {
        "mode": "direct_answer",
        "question": question,
        "answer": answer,
        "basis": basis,
    }
    return "\n".join(content_lines), payload


def build_supervisor_revised_plan_content(question: str, revised_queue: Sequence[str], reason: str) -> Tuple[str, Dict[str, Any]]:
    labels = [visible_specialist_label(agent_name) for agent_name in revised_queue]
    content_lines = [
        "Revised plan",
        f"- question: {question}",
        f"- revised specialists: {', '.join(labels) if labels else 'none'}",
        f"- reason: {reason}",
    ]
    payload = {
        "mode": "revised_plan",
        "question": question,
        "revised_queue": list(revised_queue),
        "reason": reason,
    }
    return "\n".join(content_lines), payload


async def emit_timeline_event(
    incident_id: Optional[str],
    event_type: str,
    speaker_role: str,
    title: str,
    content: str,
    payload: Optional[Dict[str, Any]] = None,
):
    if not incident_id:
        return None

    try:
        incident_uuid = uuid.UUID(str(incident_id))
        async with database.AsyncSessionLocal() as db:
            return await crud.create_incident_timeline_event(
                db,
                incident_uuid,
                event_type=event_type,
                speaker_role=speaker_role,
                title=title,
                content=content,
                payload=payload,
            )
    except Exception as e:
        logger.warning(f"Failed to emit timeline event {event_type} for {incident_id}: {e}")
        return None


async def load_pending_human_events(incident_id: Optional[str], limit: int = 1):
    if not incident_id:
        return []

    try:
        incident_uuid = uuid.UUID(str(incident_id))
        async with database.AsyncSessionLocal() as db:
            return await crud.get_pending_human_timeline_events(db, incident_uuid, limit=limit)
    except Exception as e:
        logger.warning(f"Failed to load pending human events for {incident_id}: {e}")
        return []


async def mark_human_event_handled(incident_id: Optional[str], event_id: Optional[str]) -> None:
    if not incident_id or not event_id:
        return

    try:
        event_uuid = uuid.UUID(str(event_id))
        async with database.AsyncSessionLocal() as db:
            await crud.mark_incident_timeline_event_handled(db, event_uuid)
    except Exception as e:
        logger.warning(f"Failed to mark human event handled for {incident_id}/{event_id}: {e}")