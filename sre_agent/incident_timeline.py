#!/usr/bin/env python3

import json
import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select

from backend import crud, database, models

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


def _confidence_from_response(response: str) -> str:
    """Best-effort confidence label retained only for the structured payload."""
    lower_response = (response or "").lower()
    cleaned = re.sub(r"\s+", " ", lower_response).strip()
    if not cleaned or len(cleaned.split()) <= 2:
        return "low"
    if any(token in cleaned for token in ["high confidence", "very likely", "strong evidence", "clear evidence", "confirmed"]):
        return "high"
    if any(token in cleaned for token in ["uncertain", "maybe", "might", "appears", "suggests", "likely", "no data", "unable"]):
        return "medium"
    return "medium"


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


def build_supervisor_plan_content(
    query: str,
    plan: Dict[str, Any],
    visible_queue: Sequence[str],
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the timeline event for the supervisor's initial investigation plan.

    When the caller has produced a conversational `narrative`, that text becomes
    the visible chat content. Otherwise we fall back to a short one-liner so the
    chat never displays a robotic key/value template.
    """
    visible_labels = [visible_specialist_label(agent_name) for agent_name in visible_queue]
    clean_objective = _clean_public_query(query)

    if narrative and narrative.strip():
        content = narrative.strip()
    elif visible_labels:
        first = visible_labels[0]
        content = (
            f"Got the page on {clean_objective}. I'm pulling in "
            f"{', '.join(visible_labels)} — starting with {first}."
        )
    else:
        content = f"Got the page on {clean_objective}. Triaging now."

    payload = {
        "query": query,
        "objective": clean_objective,
        "plan": plan,
        "visible_queue": list(visible_queue),
        "specialist_labels": visible_labels,
    }
    return content, payload


def build_supervisor_decision_content(
    next_agent: str,
    reasoning: str,
    remaining_agents: Sequence[str],
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the timeline event for a supervisor handoff to the next specialist."""
    visible_label = (
        visible_specialist_label(next_agent)
        if next_agent != "aggregate"
        else "Supervisor (synthesis)"
    )
    remaining_labels = [visible_specialist_label(agent_name) for agent_name in remaining_agents]

    if narrative and narrative.strip():
        content = narrative.strip()
    elif next_agent == "aggregate":
        content = "Alright, the team's reported back. Let me pull this together."
    else:
        content = f"{visible_label}, can you take this one? {reasoning}".strip()

    payload = {
        "next_agent": next_agent,
        "reasoning": reasoning,
        "remaining_agents": list(remaining_agents),
        "remaining_labels": remaining_labels,
        "next_label": visible_label,
    }
    return content, payload


def build_specialist_finding_content(
    agent_name: str,
    current_query: str,
    response: str,
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the timeline event for a specialist finding.

    The visible chat content is the conversational `narrative` produced by the
    narrator (or, in fallback, a clean snippet of the raw response). The full
    raw response is preserved in the structured payload so downstream consumers
    (e.g. the dashboard's expandable detail view) can still inspect it.
    """
    visible_label = visible_specialist_label(agent_name)
    objective = _truncate(_clean_public_query(current_query or f"Investigate {visible_label}"), 180)
    raw_response = (response or "").strip()

    if narrative and narrative.strip():
        content = narrative.strip()
    elif raw_response:
        snippet = re.sub(r"\s+", " ", raw_response)[:600].rstrip()
        if len(raw_response) > 600:
            snippet += "..."
        content = f"{visible_label} here — {snippet}"
    else:
        content = f"{visible_label} here — I didn't get any usable output from my tools on that one."

    payload = {
        "agent_name": agent_name,
        "speaker_role": visible_specialist_role(agent_name),
        "visible_label": visible_label,
        "objective": objective,
        "raw_response": _truncate(raw_response, 8000) if raw_response else "",
        "confidence": _confidence_from_response(raw_response),
    }
    return content, payload


def build_supervisor_summary_content(
    final_response: str,
    agent_results: Dict[str, Any],
    query: str = "",
    alert_context: Any = None,
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the timeline event for the supervisor's final synthesis.

    Priority for the visible chat content:
        1. `narrative` — a conversational, multi-section synthesis produced by
           the narrator (TL;DR / What we saw / Root cause / Why / Next steps).
        2. `final_response` — whatever the supervisor's aggregator produced.
        3. A minimal fallback string.

    The structured payload always lists the specialists that were invoked and
    the cleaned alert text so downstream consumers can render their own views.
    """
    objective = _truncate(_clean_public_query(query), 180) if query else "the incident"
    alert_text = _alert_context_to_text(alert_context)

    if narrative and narrative.strip():
        content = narrative.strip()
    elif final_response and final_response.strip():
        content = final_response.strip()
    else:
        content = (
            f"Wrapping up on {objective}. The specialists didn't return enough "
            "evidence for a confident root cause yet — happy to dig further if "
            "you point me at a specific signal."
        )

    payload = {
        "source": "supervisor.aggregate_responses",
        "specialists_invoked": list(agent_results.keys()),
        "objective": objective,
        "alert_context": alert_text or None,
        "raw_final_response": _truncate(final_response or "", 8000),
    }
    return content, payload


def build_supervisor_direct_answer_content(
    question: str,
    answer: str,
    basis: str,
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the timeline event for a supervisor direct answer (follow-up Q&A).

    `narrative`, when supplied, is the conversational reply that should be
    shown in the chat. Otherwise we fall back to whatever `answer` text the
    caller already produced.
    """
    if narrative and narrative.strip():
        content = narrative.strip()
    else:
        content = answer.strip() or "I can answer that directly."
    payload = {
        "mode": "direct_answer",
        "question": question,
        "answer": content,
        "basis": basis,
    }
    return content, payload


def build_supervisor_revised_plan_content(
    question: str,
    revised_queue: Sequence[str],
    reason: str,
    *,
    narrative: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    labels = [visible_specialist_label(agent_name) for agent_name in revised_queue]
    if narrative and narrative.strip():
        content = narrative.strip()
    elif labels:
        content = (
            f"Got it — re-prioritising. I'll run {', '.join(labels)} next based on "
            f"your input ({reason})."
        )
    else:
        content = (
            f"Got it — I'll keep this in mind for the next checkpoint ({reason})."
        )
    payload = {
        "mode": "revised_plan",
        "question": question,
        "revised_queue": list(revised_queue),
        "reason": reason,
        "specialist_labels": labels,
    }
    return content, payload


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


# ---------------------------------------------------------------------------
# Cross-turn context loader (used by follow-ups)
# ---------------------------------------------------------------------------


async def load_incident_chat_context(incident_id: Optional[str]) -> Dict[str, Any]:
    """Reload the conversational context for an incident from the database.

    Returned dict (all keys always present, may be empty):
        objective:       The incident title (also useful as a query stand-in).
        incident_status: e.g. "OPEN", "INVESTIGATING", "RESOLVED".
        alert_context:   A best-effort dict shaped like the original alert
                         payload (alert_name, severity, summary, ...).
        agent_results:   {agent_name: raw_response} reconstructed from prior
                         "finding" timeline events. Uses the structured
                         payload's `raw_response` when available, falling back
                         to the visible content otherwise.
        prior_summary:   The most recent supervisor "summary" event content.

    This is what the supervisor needs to answer follow-up questions in the
    same incident thread without re-running the whole investigation graph.
    """
    empty = {
        "objective": "",
        "incident_status": "",
        "alert_context": {},
        "agent_results": {},
        "prior_summary": "",
    }
    if not incident_id:
        return empty

    try:
        incident_uuid = uuid.UUID(str(incident_id))
    except (ValueError, TypeError):
        return empty

    try:
        async with database.AsyncSessionLocal() as db:
            incident = await db.get(models.Incident, incident_uuid)
            if incident is None:
                return empty

            events = await crud.get_incident_timeline_events(db, incident_uuid)
    except Exception as e:
        logger.warning(f"Failed to load incident chat context for {incident_id}: {e}")
        return empty

    objective = incident.title or "the incident"
    incident_status = ""
    if incident.status is not None:
        incident_status = (
            incident.status.value if hasattr(incident.status, "value") else str(incident.status)
        )

    agent_results: Dict[str, str] = {}
    prior_summary: str = incident.summary or ""
    alert_context: Dict[str, Any] = {
        "alert_name": incident.title,
        "severity": (
            incident.severity.value if hasattr(incident.severity, "value") else str(incident.severity)
        )
        if incident.severity is not None
        else "",
    }
    if incident.description:
        alert_context["description"] = incident.description

    for event in events:
        payload: Dict[str, Any] = {}
        if event.payload_json:
            try:
                parsed = json.loads(event.payload_json)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}

        if event.event_type == "finding":
            agent_name = payload.get("agent_name")
            raw_response = payload.get("raw_response") or event.content or ""
            if agent_name:
                agent_results[agent_name] = raw_response
        elif event.event_type == "summary":
            prior_summary = event.content or prior_summary
            payload_alert = payload.get("alert_context")
            if isinstance(payload_alert, dict):
                merged = {**alert_context, **payload_alert}
                alert_context = {k: v for k, v in merged.items() if v}
        elif event.event_type == "plan":
            plan_payload = payload.get("plan")
            if isinstance(plan_payload, dict):
                alert_context.setdefault("plan_reasoning", plan_payload.get("reasoning", ""))

    return {
        "objective": objective,
        "incident_status": incident_status,
        "alert_context": alert_context,
        "agent_results": agent_results,
        "prior_summary": prior_summary,
    }