import asyncio
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, select, update
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import crud, database, models, schemas
from backend.models import AgentAuditLog
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin
from sre_agent.api.v1.ownership import get_owned_incident
from sre_agent.approval_flow import (
    ApprovalValidationError,
    compute_action_hash,
    current_approval_interrupt,
    validate_pending_approval,
)
from sre_agent.checkpointer import durable_checkpointer_configured, thread_config
from sre_agent.narration_grounding import ground_narration
# agent_graph will be imported lazily to avoid circular dependency

router = APIRouter(
    prefix="/incidents",
    tags=["mission_control"],
    dependencies=[Depends(get_current_user_and_org)],
)

# Dependency to get the graph (to be implemented/refactored if needed)
# For now, we'll try to import it, but we might need to handle the circular dependency logic.
# A better way is to move the global `agent_graph` to a separate module 'sre_agent.globals'
# But let's try to access it via a helper or assume it's available.

async def get_agent_graph(cluster_id: uuid.UUID | str):
    from sre_agent.agent_runtime import get_agent_runtime

    try:
        return (await get_agent_runtime(cluster_id)).graph
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent system unavailable") from exc


_INVESTIGATION_KEYWORDS = (
    "alert",
    "incident",
    "error",
    "errors",
    "latency",
    "slow",
    "timeout",
    "timeouts",
    "crash",
    "fail",
    "failure",
    "cpu",
    "memory",
    "log",
    "logs",
    "metric",
    "metrics",
    "prometheus",
    "loki",
    "k8s",
    "kubernetes",
    "deploy",
    "deployment",
    "rollback",
    "restart",
    "scale",
    "investigate",
    "root cause",
    "why",
    "trace",
    "p95",
)


def _is_chat_only_message(message: str) -> bool:
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    if not normalized:
        return True

    if normalized in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}:
        return True

    if normalized.startswith(("hi ", "hello ", "hey ")):
        return True

    if normalized in {
        "what is this cluster",
        "what's this cluster",
        "what is this",
        "what's this",
        "what is happening",
        "what is happening here",
        "tell me about this cluster",
        "tell me what this is",
        "who are you",
        "what are you",
        "explain this",
    }:
        return True

    # A question about the investigation itself (the plan, findings, status,
    # why approval is needed) is asking the supervisor to explain what's
    # already known — never a reason to kick off a brand-new investigation
    # graph run. Checked before the keyword veto below, since a question like
    # "why does this need approval" or "explain the fix" legitimately
    # contains investigation vocabulary without requesting new investigation.
    # Deliberately NOT a blanket `endswith("?")` — a substantive question
    # like "what changed recently after the deploy?" still needs the keyword
    # veto below to route it into a fresh investigation instead of a reply
    # from stale context.
    if normalized.startswith(
        (
            "explain ",
            "why ",
            "why's",
            "what does",
            "what is the",
            "what's the",
            "how does",
            "how is",
            "can you explain",
        )
    ):
        return True

    if any(keyword in normalized for keyword in _INVESTIGATION_KEYWORDS):
        return False

    # Short, open-ended messages are treated as conversational unless they
    # clearly mention operational investigation terms.
    return len(normalized.split()) <= 6


def _fallback_chat_reply(message: str, incident: models.Incident, cluster: models.Cluster) -> str:
    """Deterministic fallback used only when the narrator LLM call fails.

    Kept intentionally short and informational; the primary path always goes
    through the LLM-driven narrator so the user gets a teammate-tone reply.
    """
    incident_status = str(incident.status)
    if hasattr(incident.status, "value"):
        incident_status = incident.status.value
    summary = incident.summary or incident.description or ""
    suffix = f" Status: {incident_status.replace('_', ' ').lower()}." if incident_status else ""
    if summary:
        return (
            f"On [{cluster.name}] {incident.title}.{suffix} Quick recap: "
            f"{summary[:280].rstrip()}{'...' if len(summary) > 280 else ''}"
        )
    return (
        f"On [{cluster.name}] {incident.title}.{suffix} The investigation is still gathering "
        "evidence — ask about logs, metrics, recent deploys, or the remediation plan."
    )


async def _traced_chat_reply(
    message: str,
    incident: models.Incident,
    cluster: models.Cluster,
    *,
    source: str,
    user_id: Optional[str],
) -> str:
    """Answer a chat-only message inside its own Langfuse trace.

    Both direct-reply branches — the post-summary one and the one that answers
    while the incident is still active — go through here, so neither can drift
    back out of tracing. Over Slack, the only channel, an untraced branch means
    whole turns of the human conversation simply do not exist in Langfuse.

    The reply answers from context already gathered instead of invoking the
    graph, so no callback handler runs on its own: ``trace_run`` has to carry
    the session and user attributes itself, and the narrator's own LLM call is
    attached inside ``_build_chat_reply`` via the org's handler.
    """
    from sre_agent import tracing

    incident_id = str(incident.id)
    trace_context = await _tracing_context_for_cluster(cluster.id)
    org_langfuse = trace_context.org_langfuse_credentials() if trace_context else None
    async with tracing.trace_run(
        "answer-incident-follow-up",
        org_langfuse=org_langfuse,
        input={"question": message, "incident_id": incident_id},
        metadata={
            "incident_id": incident_id,
            "mode": "direct_reply",
            # Which branch answered: the same question reads differently when
            # the incident is still running than when it is already summarised.
            "incident_status": str(getattr(incident, "status", "")),
        },
        session_id=incident_id,
        user_id=str(user_id) if user_id else None,
        tags=[f"trigger:{source}", "mode:direct-reply"],
    ) as traced_run:
        assistant_reply = await _build_chat_reply(
            message, incident, cluster, org_langfuse=org_langfuse
        )
        # This is the single seam every chat-only answer passes through, and
        # it is the one that produced 555a3acb seq 15: "we're still in the
        # investigation phase right now — the incident is marked
        # `awaiting_approval`, and the execution graph just started", which
        # was three wrong claims and no mention of `approve fix`, the only
        # reply that would have moved it. The narrator is told the status and
        # narrates around it anyway, so the correction is computed from the
        # status column and appended. Outside the `_build_chat_reply` try, so
        # the deterministic fallback gets grounded too.
        assistant_reply = ground_narration(
            assistant_reply, getattr(incident.status, "value", str(incident.status))
        )
        traced_run.set_output({"answer": assistant_reply})
    return assistant_reply


async def _build_chat_reply(
    message: str,
    incident: models.Incident,
    cluster: models.Cluster,
    *,
    org_langfuse: Optional[Dict[str, Optional[str]]] = None,
) -> str:
    """Generate a context-aware Slack-style reply for casual chat on an active incident.

    Loads the live timeline context and asks the narrator for a 1-2 sentence
    teammate-style response. Falls back to a deterministic helper only if the
    LLM call fails.
    """
    try:
        from sre_agent.incident_timeline import load_incident_chat_context
        from sre_agent.model_router import TaskType, route_llm
        from sre_agent.narrative import narrate_chat_greeting, narrate_followup_answer

        chat_context = await load_incident_chat_context(str(incident.id))
        objective = chat_context.get("objective") or incident.title
        alert_context = chat_context.get("alert_context") or {"alert_name": incident.title}
        prior_summary = chat_context.get("prior_summary") or incident.summary or ""
        incident_status = chat_context.get("incident_status", "") or str(incident.status)
        recent_turns = chat_context.get("recent_turns") or []

        llm = route_llm(TaskType.NARRATION, use_fallback=True)
        # The narrator call is the only LLM call on this path. Without the
        # Langfuse handler bound to the model the trace is a root observation
        # with nothing under it: no model, no token usage, no cost on the most
        # frequent human-in-the-loop turn there is. Bound to the model rather
        # than threaded through each narrator's signature, so adding a narrator
        # can't silently lose the generation; the handler nests it under the
        # trace_run root that _traced_chat_reply opened.
        if llm is not None and hasattr(llm, "with_config"):
            from sre_agent import tracing

            handler = tracing.get_langfuse_callback(org_langfuse)
            if handler is not None:
                llm = llm.with_config({"callbacks": [handler]})
        normalized = re.sub(r"\s+", " ", message.strip().lower())
        is_greeting = normalized in {
            "hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay", "cool", "k",
        }

        if is_greeting:
            return await narrate_chat_greeting(
                llm,
                user_message=message,
                objective=objective,
                alert_context=alert_context,
                incident_status=incident_status,
                prior_summary=prior_summary,
                recent_turns=recent_turns,
            )

        # incident.status/timeline events only move at checkpoint boundaries
        # (a new summary, a status transition), so a "what's happening right
        # now" question mid-step needs the redis-backed live execution state
        # (current_node/status, written per-node by _run_graph_impl) rather
        # than just the last persisted snapshot.
        from sre_agent.redis_state_store import get_state_store
        live_execution = get_state_store().get(str(incident.id))

        return await narrate_followup_answer(
            llm,
            question=message,
            objective=objective,
            alert_context=alert_context,
            agent_results=chat_context.get("agent_results") or {},
            prior_summary=prior_summary,
            incident_status=incident_status,
            recent_turns=recent_turns,
            live_execution=live_execution,
        )
    except Exception as exc:
        # Never let a chat reply hard-fail; produce a deterministic fallback.
        import logging
        logging.getLogger(__name__).warning(
            "Chat narrator failed for incident %s: %s", incident.id, exc
        )
        return _fallback_chat_reply(message, incident, cluster)


def _incident_is_active(incident: models.Incident) -> bool:
    return incident.status in {models.IncidentStatus.OPEN, models.IncidentStatus.INVESTIGATING}


def _incident_is_closed_for_follow_up(incident: models.Incident) -> bool:
    return incident.status == models.IncidentStatus.RESOLVED or bool(incident.summary)


async def _tracing_context_for_cluster(cluster_id: uuid.UUID | str) -> Optional[Any]:
    """Best-effort execution context (tenant Langfuse keys, cluster, model) for
    a background turn.

    Tracing must never be what breaks a Slack reply, so a missing cluster, a DB
    hiccup or an unconfigured org all degrade to "run untraced" rather than
    propagating out of a fire-and-forget task.
    """
    try:
        from sre_agent.agent_runtime import get_agent_runtime

        return (await get_agent_runtime(cluster_id)).context
    except Exception:
        return None


async def _run_post_summary_follow_up(
    incident_id: uuid.UUID,
    message: str,
    user_id: Optional[str],
    cluster_id: uuid.UUID,
) -> None:
    graph = await get_agent_graph(cluster_id)
    config = {"configurable": {"thread_id": str(incident_id)}}
    try:
        current_state = await graph.aget_state(config)
        base_values = dict(current_state.values or {}) if current_state and current_state.values else {}
    except ValueError:
        # No checkpointer configured — start follow-up with fresh state
        base_values = {}
    base_metadata = dict(base_values.get("metadata", {}))

    # Reload the canonical incident context from the database so the
    # supervisor's follow-up reasoning has the alert payload, all prior
    # specialist findings, and the prior summary — even if the LangGraph
    # checkpointer didn't keep them around between turns.
    from sre_agent.incident_timeline import load_incident_chat_context
    chat_context = await load_incident_chat_context(str(incident_id))
    prior_summary = (
        chat_context.get("prior_summary")
        or base_values.get("final_response")
        or base_metadata.get("final_response")
        or base_metadata.get("incident_summary")
    )

    follow_up_state = {
        **base_values,
        "messages": [HumanMessage(content=message)],
        "current_query": message,
        "agent_results": {},
        "agents_invoked": [],
        "current_specialist": None,
        "alert_context": (
            base_values.get("alert_context")
            or chat_context.get("alert_context")
            or {"alert_name": chat_context.get("objective", "")}
        ),
        "metadata": {
            **base_metadata,
            "incident_id": str(incident_id),
            "conversation_mode": "assistant",
            "post_investigation_follow_up": True,
            "final_response": prior_summary,
            "incident_summary": prior_summary,
            "incident_status": chat_context.get("incident_status", ""),
            "prior_findings": chat_context.get("agent_results", {}),
        },
        "incident_id": str(incident_id),
        "session_id": str(incident_id),
        "user_id": str(user_id) if user_id else None,
        "final_response": None,
    }

    # Third trace in the incident's Langfuse session, after investigate-incident
    # and resume-remediation: the on-call's follow-up question. Same session id
    # (the incident) so the whole human-in-the-loop workflow reads in order, and
    # the asker becomes the trace's user so per-responder cost/quality is visible.
    from sre_agent import tracing

    trace_context = await _tracing_context_for_cluster(cluster_id)
    org_langfuse = trace_context.org_langfuse_credentials() if trace_context else None
    config = tracing.tracing_callbacks(
        {
            **config,
            "metadata": tracing.trace_attributes(
                "answer-incident-follow-up",
                context=trace_context,
                session_id=str(incident_id),
                user_id=str(user_id) if user_id else None,
                trigger="follow-up-question",
                metadata={"incident_id": str(incident_id)},
            ),
        },
        org_langfuse,
    )

    async with tracing.trace_run(
        "answer-incident-follow-up",
        org_langfuse=org_langfuse,
        input={"question": message, "incident_id": str(incident_id)},
        metadata={"incident_id": str(incident_id)},
    ) as traced_run:
        result = await graph.ainvoke(follow_up_state, config)
        if isinstance(result, dict):
            traced_run.set_output({"answer": result.get("final_response")})


def _timeline_event_to_response(event: models.IncidentTimelineEvent) -> schemas.IncidentTimelineEventResponse:
    payload: Optional[Dict[str, Any]] = None
    if event.payload_json:
        try:
            parsed_payload = json.loads(event.payload_json)
            if isinstance(parsed_payload, dict):
                payload = parsed_payload
            else:
                payload = {"value": parsed_payload}
        except Exception:
            payload = {"raw": event.payload_json}

    return schemas.IncidentTimelineEventResponse(
        id=event.id,
        incident_id=event.incident_id,
        sequence=event.sequence,
        event_type=event.event_type,
        speaker_role=event.speaker_role,
        title=event.title,
        content=event.content,
        payload=payload,
        pending_supervisor=event.pending_supervisor,
        handled_at=event.handled_at,
        created_at=event.created_at,
    )


@router.get("/{incident_id}/transcript", response_model=schemas.IncidentTranscriptResponse)
async def get_incident_transcript(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """Get the canonical incident transcript timeline."""
    incident_uuid = uuid.UUID(incident_id)
    incident_obj = owned_incident

    events = await crud.get_incident_timeline_events(db, incident_uuid)
    conversation_mode = (
        "assistant"
        if incident_obj.status == models.IncidentStatus.RESOLVED or incident_obj.summary
        else "investigation"
    )

    return schemas.IncidentTranscriptResponse(
        incident=incident_obj,
        conversation_mode=conversation_mode,
        summary=incident_obj.summary,
        events=[_timeline_event_to_response(event) for event in events],
    )

@router.get("/{incident_id}/logs")
async def get_incident_audit_logs(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """
    Get audit logs for a specific incident.
    """
    # Fetch Audit Logs (Tools) from the migrated flight-recorder table.
    stmt = (
        select(AgentAuditLog)
        .filter(AgentAuditLog.incident_id == incident_id)
        .order_by(desc(AgentAuditLog.timestamp))
    )
    if owned_incident.cluster_id is not None:
        stmt = stmt.filter(
            (AgentAuditLog.cluster_id == owned_incident.cluster_id)
            | (AgentAuditLog.cluster_id.is_(None))
        )
    result = await db.execute(stmt)
    audit_logs = result.scalars().all()

    # Fetch Redis Logs (Thoughts/Steps)
    try:
        from sre_agent.agent_runtime import state_store
        redis_logs = state_store.get_logs(incident_id)
    except Exception:
        redis_logs = []

    # Convert Redis strings to structured objects
    structured_redis_logs = []

    for log_str in redis_logs:
        log_entry = {
            "id": str(uuid.uuid4()),
            "timestamp": None,
            "agent_name": "Supervisor",
            "tool_name": "System",
            "tool_args": log_str,
            "status": "INFO",
            "result": None,
            "error_message": None
        }

        # Try to extract timestamp: [2023-10-27T10:00:00Z] Message...
        try:
            if log_str.startswith("[") and "]" in log_str:
                ts_end = log_str.find("]")
                ts_str = log_str[1:ts_end]
                # Check if it looks like an ISO timestamp (simple check)
                if len(ts_str) > 10 and ("T" in ts_str or " " in ts_str):
                     # Parse to ensure validity, but keep string for UI
                     # fromisoformat might fail on 'Z', so we might need replacement if < 3.11
                     from datetime import datetime
                     # Minimal validation
                     log_entry["timestamp"] = ts_str
                     # Clean the message: Remove [timestamp] prefix
                     # [timestamp] Message -> Message
                     if len(log_str) > ts_end + 1:
                         log_entry["tool_args"] = log_str[ts_end + 1:].strip()
        except Exception:
            pass

        structured_redis_logs.append(log_entry)

    combined_logs = []
    for log in audit_logs:
        combined_logs.append({
            "id": str(log.id),
            "timestamp": log.timestamp.isoformat(),
            "agent_name": log.agent_name,
            "tool_name": log.tool_name,
            "tool_args": log.tool_args,
            "status": log.status,
            "result": log.result,
            "error_message": log.error_message
        })

    for r_log in structured_redis_logs:
        combined_logs.append(r_log)

    # Sort combined logs by timestamp
    def get_sort_key(x):
        ts = x.get("timestamp")
        if not ts:
            return ""
        return ts

    combined_logs.sort(key=get_sort_key, reverse=True)

    return combined_logs


async def handle_incident_message(
    db: AsyncSession,
    incident: models.Incident,
    cluster: models.Cluster,
    message: str,
    *,
    source: str,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Post a follow-up message for an incident and queue a new investigation turn.

    Reached from the Slack war room alone (`war_room.route_thread_reply`). The
    dashboard route that also called this is gone: it queued a full agent turn
    for any org member and no mounted component reached it. `source` is
    required rather than defaulted, so a second surface has to name itself in
    the timeline instead of inheriting a label from a route that no longer
    exists; `user_id` stays best-effort (Slack messages carry no JWT).
    """
    message = message.strip()
    if not message:
        raise ValueError("Message cannot be empty")

    incident_uuid = incident.id
    incident_id = str(incident_uuid)

    if _incident_is_closed_for_follow_up(incident):
        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="human_message",
            speaker_role="user",
            title="You",
            content=message,
            payload={"source": source, "mode": "post_summary_follow_up"},
        )

        from sre_agent.redis_state_store import get_state_store
        state_store = get_state_store()
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
        )

        # A pure Q&A follow-up ("explain the fix", "why does this need
        # approval") is answered immediately from context already gathered
        # (findings, prior summary, plan reasoning) instead of re-running the
        # full investigation graph, which produces a fresh plan/summary, not
        # an answer to the question actually asked.
        if _is_chat_only_message(message):
            assistant_reply = await _traced_chat_reply(
                message, incident, cluster, source=source, user_id=user_id
            )

            await crud.create_incident_timeline_event(
                db,
                incident_uuid,
                event_type="assistant_message",
                speaker_role="supervisor",
                title="Supervisor",
                content=assistant_reply,
                payload={"source": source, "mode": "direct_reply"},
            )
            state_store.append_log(
                incident_id,
                f"[{datetime.now(timezone.utc).isoformat()}] ASSISTANT: {assistant_reply}"
            )

            return {
                "status": "RESPONDED",
                "incident_id": incident_id,
                "response": assistant_reply,
            }

        asyncio.create_task(
            _run_post_summary_follow_up(
                incident_uuid,
                message,
                user_id,
                incident.cluster_id,
            )
        )

        return {
            "status": "FOLLOW_UP_QUEUED",
            "incident_id": incident_id,
            "conversation_mode": "assistant",
        }

    if _incident_is_active(incident) and not _is_chat_only_message(message):
        queued_event = await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="human_message",
            speaker_role="user",
            title="You",
            content=message,
            payload={
                "source": source,
                "mode": "pending_supervisor",
            },
            pending_supervisor=True,
        )

        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="system_event",
            speaker_role="system",
            title="System",
            content="Human input queued for the next supervisor checkpoint.",
            payload={
                "source": source,
                "mode": "queued_for_supervisor",
                "pending_event_id": str(queued_event.id),
            },
        )

        from sre_agent.redis_state_store import get_state_store
        state_store = get_state_store()
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
        )
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] SYSTEM: queued for supervisor checkpoint"
        )

        return {
            "status": "PENDING_SUPERVISOR",
            "incident_id": incident_id,
            "message": "Queued for the next safe supervisor checkpoint.",
        }

    if _is_chat_only_message(message):
        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="human_message",
            speaker_role="user",
            title="You",
            content=message,
            payload={"source": source, "mode": "incoming"},
        )

        assistant_reply = await _traced_chat_reply(
            message, incident, cluster, source=source, user_id=user_id
        )

        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="assistant_message",
            speaker_role="supervisor",
            title="Supervisor",
            content=assistant_reply,
            payload={"source": source, "mode": "direct_reply"},
        )

        from sre_agent.redis_state_store import get_state_store
        state_store = get_state_store()
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
        )
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] ASSISTANT: {assistant_reply}"
        )

        return {
            "status": "RESPONDED",
            "incident_id": incident_id,
            "response": assistant_reply,
        }

    human_event = await crud.create_incident_timeline_event(
        db,
        incident_uuid,
        event_type="human_message",
        speaker_role="user",
        title="You",
        content=message,
        payload={"source": source, "mode": "incoming"},
    )

    from sre_agent.redis_state_store import get_state_store
    state_store = get_state_store()
    state_store.append_log(
        incident_id,
        f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
    )

    # Reuse the original alert scope and enqueue one durable follow-up turn.
    from sre_agent.incident_timeline import load_incident_chat_context
    from sre_agent.job_worker import enqueue_and_kick

    follow_up_context = await load_incident_chat_context(str(incident_uuid))
    follow_up_alert = follow_up_context.get("alert_context") or {}
    follow_up_job = await enqueue_and_kick(
        db=db,
        cluster_id=cluster.id,
        organization_id=cluster.org_id,
        incident_id=incident_uuid,
        alert_name=message,
        alert_labels=follow_up_alert.get("labels") or {},
        alert_annotations={
            "summary": follow_up_alert.get("summary", ""),
            "description": follow_up_alert.get("description", ""),
        },
        alert_starts_at=None,
        alert_severity=follow_up_alert.get("severity") or "warning",
        triggered_by=source,
        idempotency_key=f"follow-up:{incident_uuid}:{human_event.id}",
    )

    await crud.create_incident_timeline_event(
        db,
        incident_uuid,
        event_type="system_event",
        speaker_role="system",
        title="System",
        content="Follow-up queued for investigation.",
        payload={
            "source": source,
            "mode": "queued_investigation",
            "job_id": str(follow_up_job.id),
            "cluster_id": str(cluster.id),
        },
    )

    return {
        "status": "QUEUED",
        "incident_id": incident_id,
        "job_id": str(follow_up_job.id),
    }


@router.get("/{incident_id}/status")
async def get_incident_status(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """
    Get the current status of the LangGraph execution for this incident.
    """
    graph = await get_agent_graph(owned_incident.cluster_id)
    config = {"configurable": {"thread_id": incident_id}}

    try:
        current_state = await graph.aget_state(config)

        if not current_state.values:
             return {"status": "UNKNOWN", "next": []}

        next_ops = current_state.next

        interrupt_payload = current_approval_interrupt(current_state)
        is_paused = interrupt_payload is not None

        return {
            "status": "WAITING_APPROVAL" if is_paused else "RUNNING",
            "next": next_ops,
            "values": current_state.values,
            "approval": interrupt_payload,
            "created_at": current_state.created_at
        }
    except Exception as e:
        # State might not exist yet
        return {"status": "NOT_STARTED", "error": str(e)}


@router.get("/{incident_id}/agent-metrics")
async def get_incident_agent_metrics(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """Per-incident node telemetry plus fail-closed root-run evidence."""
    from sre_agent.model_accounting import get_model_accounting_recorder
    from sre_agent.observability import get_recorder
    from sre_agent.trace_evidence import get_run_trace_recorder

    accounting_recorder = get_model_accounting_recorder()
    trace_recorder = get_run_trace_recorder()
    root_trace_ids = trace_recorder.root_trace_ids(incident_id=incident_id)
    model_accounting = accounting_recorder.summary(incident_id=incident_id)
    trace_completeness = {
        "complete": False,
        "completeness_reasons": ["root_trace_not_recorded"],
        "root_trace_id": None,
        "spans": 0,
        "cost_usd": None,
        "tokens": None,
    }
    if root_trace_ids:
        root_trace_id = str(root_trace_ids[-1])
        model_accounting = accounting_recorder.summary(
            root_trace_id=root_trace_id
        )
        trace_completeness = trace_recorder.summary(
            root_trace_id=root_trace_id,
            model_accounting=model_accounting,
        )

    return {
        **get_recorder().summary(incident_id),
        "model_accounting": model_accounting,
        "trace_completeness": trace_completeness,
    }


@router.post("/{incident_id}/approve")
async def approve_incident_action(
    incident_id: str,
    approval: schemas.ApprovalDecisionRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """Atomically authorize and synchronously resume one exact graph action."""
    await require_admin(user)
    if not durable_checkpointer_configured():
        raise HTTPException(
            status_code=503,
            detail="A durable checkpointer is required for approvals",
        )

    locked_result = await db.execute(
        select(models.Incident)
        .where(
            models.Incident.id == owned_incident.id,
            models.Incident.cluster_id == owned_incident.cluster_id,
        )
        .with_for_update()
    )
    locked_incident = locked_result.scalar_one_or_none()
    if (
        locked_incident is not None
        and locked_incident.status == models.IncidentStatus.RESOLVED
    ):
        await db.execute(
            update(models.ApprovalRequest)
            .where(
                models.ApprovalRequest.id == approval.approval_request_id,
                models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            )
            .values(
                status=models.ApprovalStatus.EXPIRED,
                decided_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail="The incident is resolved; this approval was withdrawn",
        )

    result = await db.execute(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.id == approval.approval_request_id,
            models.ApprovalRequest.incident_id == owned_incident.id,
            models.ApprovalRequest.organization_id == user.org_id,
            models.ApprovalRequest.cluster_id == owned_incident.cluster_id,
        )
    )
    pending = result.scalar_one_or_none()
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    now = datetime.now(timezone.utc)
    try:
        validate_pending_approval(
            status=pending.status,
            stored_action_hash=pending.action_hash,
            submitted_action_hash=approval.action_hash,
            expires_at=pending.expires_at,
            now=now,
        )
    except ApprovalValidationError as exc:
        if exc.reason == "not_pending":
            raise HTTPException(
                status_code=409, detail="Approval request is no longer pending"
            ) from exc
        if exc.reason == "hash_mismatch":
            raise HTTPException(status_code=400, detail="Action hash does not match") from exc
        await db.execute(
            update(models.ApprovalRequest)
            .where(
                models.ApprovalRequest.id == pending.id,
                models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            )
            .values(status=models.ApprovalStatus.EXPIRED, decided_at=now)
        )
        await db.commit()
        raise HTTPException(status_code=410, detail="Approval request expired") from exc

    from sre_agent.agent_runtime import get_agent_runtime

    try:
        runtime = await get_agent_runtime(owned_incident.cluster_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent system unavailable") from exc
    graph = runtime.graph
    from sre_agent import tracing

    org_langfuse = runtime.context.org_langfuse_credentials()
    # Same Langfuse session as the investigation that produced this plan (the
    # incident id), so the tracing UI shows the whole human-in-the-loop
    # workflow in order. The trace name matches the Slack path's resume because
    # it is the same operation — only the trigger tag differs, which is exactly
    # the dimension you'd want to compare dashboard vs. Slack approvals on.
    config = thread_config(
        pending.thread_id,
        {
            "metadata": tracing.trace_attributes(
                "resume-remediation",
                context=runtime.context,
                session_id=str(owned_incident.id),
                user_id=str(user.id),
                trigger="dashboard-approval",
                metadata={
                    "incident_id": str(owned_incident.id),
                    "approval_request_id": str(pending.id),
                    "action_hash": pending.action_hash,
                },
            ),
        },
        org_langfuse=org_langfuse,
    )
    configurable = (config or {}).get("configurable", {})
    if configurable.get("thread_id") != pending.thread_id:
        raise HTTPException(
            status_code=503,
            detail="Durable checkpointing is required for approvals",
        )

    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Pending graph interrupt unavailable") from exc

    interrupt_payload = current_approval_interrupt(snapshot)
    if not interrupt_payload:
        raise HTTPException(status_code=409, detail="No approval interrupt is pending")
    interrupt_report = interrupt_payload.get("report")
    if not isinstance(interrupt_report, dict):
        raise HTTPException(status_code=409, detail="Approval interrupt is invalid")
    current_hash = compute_action_hash(interrupt_report)
    if (
        str(interrupt_payload.get("approval_request_id")) != str(pending.id)
        or str(interrupt_payload.get("thread_id")) != pending.thread_id
        or not secrets.compare_digest(
            str(interrupt_payload.get("action_hash", "")), pending.action_hash
        )
        or not secrets.compare_digest(current_hash, pending.action_hash)
    ):
        raise HTTPException(
            status_code=409,
            detail="Approval does not match the current graph interrupt",
        )

    cas = await db.execute(
        update(models.ApprovalRequest)
        .where(
            models.ApprovalRequest.id == pending.id,
            models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
            models.ApprovalRequest.action_hash == approval.action_hash,
            models.ApprovalRequest.expires_at > now,
        )
        .values(
            status=models.ApprovalStatus.APPROVED,
            approver_user_id=user.id,
            decided_at=now,
        )
    )
    if cas.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Approval was already decided")
    await db.commit()

    try:
        async with tracing.trace_run(
            "resume-remediation",
            org_langfuse=org_langfuse,
            input={
                "approved_plan": (interrupt_report or {}).get("actions") or interrupt_report,
                "approved_by": str(user.id),
                "incident_id": str(owned_incident.id),
            },
            metadata={
                "incident_id": str(owned_incident.id),
                "approval_request_id": str(pending.id),
            },
        ) as traced_run:
            output = await graph.ainvoke(
                Command(
                    resume={
                        "approved": True,
                        "approval_request_id": str(pending.id),
                        "action_hash": pending.action_hash,
                    }
                ),
                config=config,
            )
            if isinstance(output, dict):
                traced_run.set_output(
                    {
                        "act_report": (output.get("metadata") or {}).get("act_report"),
                        "summary": output.get("final_response"),
                    }
                )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Approved action failed to resume") from exc

    if isinstance(output, dict):
        from sre_agent.incident_status import (
            compute_incident_status,
            effective_status_after_run,
            resolved_at_for_status,
        )

        act_report = (output.get("metadata") or {}).get("act_report")
        verification = (act_report or {}).get("verification")
        computed_status = compute_incident_status(output, act_report, verification)
        current_result = await db.execute(
            select(models.Incident)
            .where(models.Incident.id == owned_incident.id)
            .with_for_update()
        )
        current_incident = current_result.scalar_one_or_none()
        current_status = getattr(current_incident, "status", None)
        effective_status = effective_status_after_run(
            current_status, computed_status
        )
        incident_values: Dict[str, Any] = {"status": effective_status}
        if current_status != models.IncidentStatus.RESOLVED:
            incident_values["resolved_at"] = resolved_at_for_status(
                effective_status, datetime.now(timezone.utc)
            )
        await db.execute(
            update(models.Incident)
            .where(models.Incident.id == owned_incident.id)
            .values(**incident_values)
        )
        await db.commit()

    return {
        "status": "RESUMED",
        "approval_request_id": str(pending.id),
        "thread_id": pending.thread_id,
        "completed": bool(output),
    }


@router.post("/{incident_id}/mark-resolved")
async def mark_incident_resolved(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """On-call's manual confirmation that an incident is actually fixed, once
    they've taken over from automated remediation and verified it themselves
    (e.g. after IncidentRemediationWorkflow exhausted its retries and closed
    with REMEDIATION_FAILED / "needs manual review"). Deliberately no
    automated precondition on the current status: a human who has manually
    checked the system is the authority here, not the pipeline's own state.

    Shares ``mark_incident_resolved_by_human`` with Slack's "mark resolved"
    reply so both surfaces also close the war room and publish the resolved
    lifecycle event — resolving here used to write the status and nothing
    else, leaving the incident's Slack thread live and the dashboards showing
    it open.
    """
    await require_admin(user)
    from sre_agent.approval_flow import mark_incident_resolved_by_human

    await mark_incident_resolved_by_human(
        incident_id=str(owned_incident.id),
        organization_id=str(user.org_id),
        cluster_id=str(owned_incident.cluster_id),
        actor=getattr(user, "email", None),
    )
    await db.refresh(owned_incident)
    return {"status": "RESOLVED", "incident_id": str(owned_incident.id)}
