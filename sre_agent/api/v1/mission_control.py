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
from sqlalchemy.exc import ProgrammingError
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import crud, database, models, schemas
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin
from sre_agent.api.v1.ownership import get_owned_incident
from sre_agent.approval_flow import (
    ApprovalValidationError,
    compute_action_hash,
    current_approval_interrupt,
    validate_pending_approval,
)
from sre_agent.checkpointer import durable_checkpointer_configured, thread_config
from sre_agent.models import AgentAuditLog
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
    status = str(incident.status)
    if hasattr(incident.status, "value"):
        status = incident.status.value
    summary = incident.summary or incident.description or ""
    suffix = f" Status: {status.replace('_', ' ').lower()}." if status else ""
    if summary:
        return (
            f"On [{cluster.name}] {incident.title}.{suffix} Quick recap: "
            f"{summary[:280].rstrip()}{'...' if len(summary) > 280 else ''}"
        )
    return (
        f"On [{cluster.name}] {incident.title}.{suffix} The investigation is still gathering "
        "evidence — ask about logs, metrics, recent deploys, or the remediation plan."
    )


async def _build_chat_reply(message: str, incident: models.Incident, cluster: models.Cluster) -> str:
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

        llm = route_llm(TaskType.NARRATION, use_fallback=True)
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
            )

        return await narrate_followup_answer(
            llm,
            question=message,
            objective=objective,
            alert_context=alert_context,
            agent_results=chat_context.get("agent_results") or {},
            prior_summary=prior_summary,
            incident_status=incident_status,
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


async def _run_post_summary_follow_up(
    incident_id: uuid.UUID,
    message: str,
    user: models.User,
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
    owned_incident: models.Incident = Depends(get_owned_incident),
):
    """
    Post a follow-up message for an incident and queue a new investigation turn.
    """
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    incident_uuid = uuid.UUID(incident_id)
    incident_obj = owned_incident
    # Direct unit calls do not resolve FastAPI dependencies. Keep those calls
    # on the same centralized authorization path instead of duplicating a load.
    if not hasattr(incident_obj, "id"):
        incident_obj = await get_owned_incident(incident_uuid, user, db)

    cluster = await crud.get_cluster_by_id(db, incident_obj.cluster_id)
    if not cluster:
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

        asyncio.create_task(
            _run_post_summary_follow_up(
                incident_uuid,
                message,
                user,
                incident_obj.cluster_id,
            )
        )

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

        assistant_reply = await _build_chat_reply(message, incident_obj, cluster)

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
    # For follow-up investigations on an incident, reuse the original alert's
    # labels and annotations so the specialists keep the same context they
    # had during the first pass.
    from sre_agent.incident_timeline import load_incident_chat_context
    follow_up_context = await load_incident_chat_context(str(incident_uuid))
    follow_up_alert = follow_up_context.get("alert_context") or {}
    asyncio.create_task(
        run_graph_background_saas(
            incident_id=incident_uuid,
            cluster_id=cluster.id,
            alert_name=message,
            job_id=follow_up_job.id,
            alert_labels=follow_up_alert.get("labels") or {},
            alert_annotations={
                "summary": follow_up_alert.get("summary", ""),
                "description": follow_up_alert.get("description", ""),
            },
            alert_starts_at=None,
            alert_severity=follow_up_alert.get("severity") or "warning",
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

    graph = await get_agent_graph(owned_incident.cluster_id)
    config = thread_config(pending.thread_id)
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Approved action failed to resume") from exc

    if isinstance(output, dict):
        from sre_agent.incident_status import compute_incident_status

        act_report = (output.get("metadata") or {}).get("act_report")
        verification = (act_report or {}).get("verification")
        computed_status = compute_incident_status(output, act_report, verification)
        incident_values: Dict[str, Any] = {"status": computed_status}
        if computed_status == models.IncidentStatus.RESOLVED:
            incident_values["resolved_at"] = datetime.now(timezone.utc)
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
