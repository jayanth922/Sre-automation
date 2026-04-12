import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, select
from sqlalchemy.exc import ProgrammingError
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import crud, database, models, schemas
from backend.rbac import require_admin
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.models import AgentAuditLog
# agent_graph will be imported lazily to avoid circular dependency

router = APIRouter(
    prefix="/incidents",
    tags=["mission_control"],
)

# Dependency to get the graph (to be implemented/refactored if needed)
# For now, we'll try to import it, but we might need to handle the circular dependency logic.
# A better way is to move the global `agent_graph` to a separate module 'sre_agent.globals'
# But let's try to access it via a helper or assume it's available.

def get_agent_graph():
    from sre_agent.agent_runtime import agent_graph
    if agent_graph is None:
        raise HTTPException(status_code=503, detail="Agent system not initialized")
    return agent_graph


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

    if any(keyword in normalized for keyword in _INVESTIGATION_KEYWORDS):
        return False

    # Short, open-ended messages are treated as conversational unless they
    # clearly mention operational investigation terms.
    return len(normalized.split()) <= 6


def _build_chat_reply(message: str, incident: models.Incident, cluster: models.Cluster) -> str:
    normalized = re.sub(r"\s+", " ", message.strip().lower())

    if normalized in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}:
        return (
            f"I'm tracking [{cluster.name}] {incident.title}. Ask about logs, metrics, the suspected cause, "
            f"or the remediation plan, and I’ll answer in this thread."
        )

    if "cluster" in normalized or "what is this" in normalized or "what is happening" in normalized:
        status = incident.status.replace("_", " ").title()
        summary = incident.summary or incident.description or "No summary is available yet."
        return (
            f"This is the {cluster.name} incident thread for [{cluster.name}] {incident.title}. "
            f"Current incident status: {status}. {summary}"
        )

    return (
        f"I’m here to help with [{cluster.name}] {incident.title}. Ask me about the incident, logs, metrics, "
        f"or remediation steps."
    )


def _incident_is_active(incident: models.Incident) -> bool:
    return incident.status in {models.IncidentStatus.OPEN, models.IncidentStatus.INVESTIGATING}


def _incident_is_closed_for_follow_up(incident: models.Incident) -> bool:
    return incident.status == models.IncidentStatus.RESOLVED or bool(incident.summary)


async def _run_post_summary_follow_up(
    incident_id: uuid.UUID,
    message: str,
    user: models.User,
) -> None:
    graph = get_agent_graph()
    config = {"configurable": {"thread_id": str(incident_id)}}
    current_state = await graph.aget_state(config)
    base_values = dict(current_state.values or {}) if current_state and current_state.values else {}
    base_metadata = dict(base_values.get("metadata", {}))

    follow_up_state = {
        **base_values,
        "messages": [HumanMessage(content=message)],
        "current_query": message,
        "agent_results": {},
        "agents_invoked": [],
        "current_specialist": None,
        "metadata": {
            **base_metadata,
            "incident_id": str(incident_id),
            "conversation_mode": "assistant",
            "post_investigation_follow_up": True,
            "final_response": base_values.get("final_response") or base_metadata.get("final_response") or base_metadata.get("incident_summary"),
        },
        "incident_id": str(incident_id),
        "session_id": str(incident_id),
        "user_id": str(user.id),
        "final_response": None,
    }

    await graph.ainvoke(follow_up_state, config)


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
):
    """Get the canonical incident transcript timeline."""
    incident_uuid = uuid.UUID(incident_id)
    incident = await db.execute(
        select(models.Incident).filter(models.Incident.id == incident_uuid)
    )
    incident_obj = incident.scalars().first()
    if not incident_obj:
        raise HTTPException(status_code=404, detail="Incident not found")

    cluster = await crud.get_cluster_by_id(db, incident_obj.cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Incident not found")

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
):
    """
    Get audit logs for a specific incident.
    """
    # Fetch Audit Logs (Tools)
    audit_logs = []
    try:
        stmt = select(AgentAuditLog).filter(
            AgentAuditLog.incident_id == incident_id
        ).order_by(desc(AgentAuditLog.timestamp))
        result = await db.execute(stmt)
        audit_logs = result.scalars().all()
    except ProgrammingError:
        audit_logs = []

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


@router.post("/{incident_id}/message")
async def send_incident_message(
    incident_id: str,
    payload: schemas.IncidentMessageRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """
    Post a follow-up message for an incident and queue a new investigation turn.
    """
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    incident_uuid = uuid.UUID(incident_id)
    incident = await db.execute(
        select(models.Incident).filter(models.Incident.id == incident_uuid)
    )
    incident_obj = incident.scalars().first()
    if not incident_obj:
        raise HTTPException(status_code=404, detail="Incident not found")

    cluster = await crud.get_cluster_by_id(db, incident_obj.cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Incident not found")

    if _incident_is_closed_for_follow_up(incident_obj):
        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="human_message",
            speaker_role="user",
            title="You",
            content=message,
            payload={"source": "dashboard_chat", "mode": "post_summary_follow_up"},
        )

        from sre_agent.redis_state_store import get_state_store
        state_store = get_state_store()
        state_store.append_log(
            incident_id,
            f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
        )

        asyncio.create_task(_run_post_summary_follow_up(incident_uuid, message, user))

        return {
            "status": "FOLLOW_UP_QUEUED",
            "incident_id": incident_id,
            "conversation_mode": "assistant",
        }

    if _incident_is_active(incident_obj) and not _is_chat_only_message(message):
        queued_event = await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="human_message",
            speaker_role="user",
            title="You",
            content=message,
            payload={
                "source": "dashboard_chat",
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
                "source": "dashboard_chat",
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
            payload={"source": "dashboard_chat", "mode": "incoming"},
        )

        assistant_reply = _build_chat_reply(message, incident_obj, cluster)

        await crud.create_incident_timeline_event(
            db,
            incident_uuid,
            event_type="assistant_message",
            speaker_role="supervisor",
            title="Supervisor",
            content=assistant_reply,
            payload={"source": "dashboard_chat", "mode": "direct_reply"},
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

    await crud.create_incident_timeline_event(
        db,
        incident_uuid,
        event_type="human_message",
        speaker_role="user",
        title="You",
        content=message,
        payload={"source": "dashboard_chat", "mode": "incoming"},
    )

    from sre_agent.redis_state_store import get_state_store
    state_store = get_state_store()
    state_store.append_log(
        incident_id,
        f"[{datetime.now(timezone.utc).isoformat()}] USER: {message}"
    )

    follow_up_job = await crud.create_job(
        db,
        cluster.id,
        schemas.JobCreate(
            job_type=models.JobType.INVESTIGATION,
            payload=json.dumps({
                "incident_id": incident_id,
                "alert": message,
                "triggered_by": "dashboard_chat",
                "follow_up": True,
            }),
        ),
    )

    await crud.create_incident_timeline_event(
        db,
        incident_uuid,
        event_type="system_event",
        speaker_role="system",
        title="System",
        content="Follow-up queued for investigation.",
        payload={
            "source": "dashboard_chat",
            "mode": "queued_investigation",
            "job_id": str(follow_up_job.id),
            "cluster_id": str(cluster.id),
        },
    )

    from sre_agent.agent_runtime import run_graph_background_saas
    asyncio.create_task(
        run_graph_background_saas(
            incident_id=incident_uuid,
            cluster_id=cluster.id,
            alert_name=message,
            job_id=follow_up_job.id,
        )
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
):
    """
    Get the current status of the LangGraph execution for this incident.
    """
    graph = get_agent_graph()
    config = {"configurable": {"thread_id": incident_id}}

    try:
        current_state = await graph.aget_state(config)

        if not current_state.values:
             return {"status": "UNKNOWN", "next": []}

        next_ops = current_state.next

        # Check if we are waiting for input (interrupted)
        is_paused = False
        if next_ops:
            # If next step is 'execute_action' and we have tasks, it might be paused via interrupt_before
            # LangGraph StateSnapshot has 'tasks' which are pending
            if current_state.tasks:
                first_task = current_state.tasks[0]
                if first_task.interrupts:
                    is_paused = True

        return {
            "status": "WAITING_APPROVAL" if is_paused else "RUNNING",
            "next": next_ops,
            "values": current_state.values,
            "created_at": current_state.created_at
        }
    except Exception as e:
        # State might not exist yet
        return {"status": "NOT_STARTED", "error": str(e)}

@router.post("/{incident_id}/approve")
async def approve_incident_action(
    incident_id: str,
    user: models.User = Depends(get_current_user_and_org),
):
    """
    Resume execution with approval. Admin only.
    """
    require_admin(user)
    graph = get_agent_graph()
    config = {"configurable": {"thread_id": incident_id}}

    try:
        # Resume the graph
        # output = await graph.ainvoke(Command(resume="APPROVE"), config)
        # Actually, for resuming from interrupt, we update state or just invoke with Command

        # NOTE: If we are just resuming, we can use None or a specific value expected by the graph
        # If using interrupt_before, we typically just run it again?
        # No, we need to invoke. Providing Command(resume="APPROVE") is correct if we used interrupt(payload)
        # If we used interrupt_before=["node"], we just need to continue.
        # But usually 'interrupt_before' stops *before* the node. To run it, we just invoke(None, config)?
        # Or invoke(Command(resume=...), ...) if we want to change behavior?

        # Let's assume we used a simple interrupt_before logic.
        # But if the user request says: "Resume execution using graph.invoke(Command(resume='APPROVE'), config)"
        # I will follow that instruction.

        background_task_run = asyncio.create_task(
            graph.ainvoke(Command(resume="APPROVE"), config)
        )
        # We don't await full completion to return fast

        return {"status": "RESUMED"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
