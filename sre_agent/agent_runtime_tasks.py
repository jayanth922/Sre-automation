"""QUARANTINED alternate investigation runner.

This module historically duplicated SaaS graph execution with a divergent
feature set (lifecycle publishes, per-cluster metadata). Production must not
import it for new work.

Use::

    from sre_agent.incident_runner import run_incident_investigation

The symbols below remain only as deprecated forwarders for accidental imports.
"""

from __future__ import annotations

import uuid
import warnings
from typing import Optional

from sre_agent.incident_runner import run_incident_investigation

__all__ = ["run_graph_background_saas"]


async def run_graph_background_saas(
    incident_id: uuid.UUID,
    cluster_id: uuid.UUID,
    alert_name: str,
    job_id: Optional[uuid.UUID] = None,
    **kwargs,
):
    """
    SaaS-aware background execution.
    Writes logs/results to the Postgres Database instead of just Redis.
    """
    from .agent_runtime import initialize_agent
    session_id = str(incident_id)
    state_store = get_state_store()
    
    logger.info(f"▶️ Starting SaaS background graph execution for incident: {incident_id} (Job: {job_id})")
    
    # Update Incident Status to INVESTIGATING and Job to RUNNING
    async with database.AsyncSessionLocal() as db:
        # Update Incident
        await db.execute(
            models.Incident.__table__
            .update()
            .where(models.Incident.id == incident_id)
            .values(status=IncidentStatus.INVESTIGATING)
        )

        # Update Job if provided
        if job_id:
            await db.execute(
                models.Job.__table__
                .update()
                .where(models.Job.id == job_id)
                .values(
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(timezone.utc),
                    logs=f"[{datetime.now(timezone.utc).isoformat()}] Agent investigation started.\n"
                )
            )
        await db.commit()

    try:
        runtime = await initialize_agent(cluster_id)
        agent_graph, tools = runtime.graph, runtime.tools
        
        from langchain_core.messages import HumanMessage

        # Resolve the authorized agent "brain" from the tenant execution context.
        # Provider/model/base_url/key are enforced against operator allowlists and
        # recorded exactly in run metadata for UI/trace parity.
        llm_manifest = runtime.context.llm_manifest()
        llm_provider = llm_manifest["provider"] or os.getenv("LLM_PROVIDER", "groq")

        initial_state: AgentState = {
            "messages": [HumanMessage(content=f"Investigate alert: {alert_name}")],
            "ooda_phase": "OBSERVE",
            "next": "investigation_swarm",
            "agent_results": {},
            "current_query": f"Investigate alert: {alert_name}",
            "metadata": {
                "llm_provider": llm_provider,
                "llm": llm_manifest,
                "llm_overrides": {
                    "provider": llm_manifest["provider"],
                    "model": llm_manifest["model"],
                    "base_url": llm_manifest["base_url"],
                    "api_key": None,
                },
                "cluster_namespace": runtime.context.namespace,
                "cluster_environment": runtime.context.environment,
                "tools": tools,
                "cluster_id": str(cluster_id),
                "incident_id": str(incident_id),
            },
            "requires_collaboration": True,
            "agents_invoked": [],
            "final_response": None,
            "auto_approve_plan": True,
            "session_id": session_id,
            "user_id": "saas_user",
        }
        
        # Redis Logging Setup
        state_store.set(session_id, {
            "status": "RUNNING",
            "current_node": "start",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        callback_handler = RedisLogCallbackHandler(session_id)
        current_execution_state = initial_state

        # Announce the incident on the live bus so the Slack service can open a
        # two-way war-room thread for it (design slice #2). Best-effort.
        try:
            from .live_events import publish_lifecycle_event

            org_id = None
            if cluster_obj is not None and getattr(cluster_obj, "org_id", None):
                org_id = str(cluster_obj.org_id)
            await publish_lifecycle_event(
                "opened",
                incident_id=str(incident_id),
                alert_name=alert_name,
                summary=f"Investigating alert: {alert_name}",
                org_id=org_id,
            )
        except Exception as _bus_err:
            logger.debug(f"incident-open publish skipped: {_bus_err}")

        from .checkpointer import thread_config
        from .tracing import tracing_callbacks
        # thread_config adds durability (thread_id); tracing_callbacks adds the
        # Langfuse handler when configured. Both no-op by default.
        _config = tracing_callbacks(thread_config(str(incident_id), {"callbacks": [callback_handler]}))
        async for event in agent_graph.astream(initial_state, config=_config):
            for node_name, node_output in event.items():
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_line = f"[{timestamp}] 🤖 AGENT_{node_name.upper()}: Step execution started."
                
                if node_name == "investigation_swarm":
                    log_line = f"[{timestamp}] 🔍 INVESTIGATION: Querying K8s, Metrics, and Logs in parallel..."
                elif node_name == "reflector":
                    log_line = f"[{timestamp}] 🧠 REFLECTOR: Correlating findings and forming hypothesis..."
                elif node_name == "supervisor":
                    log_line = f"[{timestamp}] 🧭 SUPERVISOR: Reviewing evidence and choosing the next specialist..."
                elif node_name == "aggregate":
                    log_line = f"[{timestamp}] 🧭 SUPERVISOR: Synthesizing specialist findings into the final summary..."
                
                state_store.append_log(session_id, log_line)

                if job_id:
                    try:
                        async with database.AsyncSessionLocal() as db:
                            from sqlalchemy import update, func
                            await db.execute(
                                update(models.Job)
                                .where(models.Job.id == job_id)
                                .values(
                                    logs=func.concat(func.coalesce(models.Job.logs, ""), log_line + "\n"),
                                    status=JobStatus.RUNNING
                                )
                            )
                            await db.commit()
                    except Exception as le:
                        logger.warning(f"Failed to sync thought log to job: {le}")
                
                # Guard against None node_output from failed/empty graph nodes
                if node_output is not None and isinstance(node_output, dict):
                    current_execution_state = {**current_execution_state, **node_output}

        # Extract the final response written by the aggregate node
        final_response = current_execution_state.get("final_response") or "Investigation completed."

        # Extract plan if it exists and convert to serializable format
        raw_plan = current_execution_state.get("remediation_plan")
        remediation_plan_serializable = []
        
        if raw_plan:
            # Handle Pydantic model (preferred)
            if hasattr(raw_plan, "model_dump"):
                remediation_plan_serializable = [raw_plan.model_dump()]
            elif hasattr(raw_plan, "dict"):
                remediation_plan_serializable = [raw_plan.dict()]
            # Handle list of actions (legacy or string)
            elif isinstance(raw_plan, list):
                remediation_plan_serializable = raw_plan
            elif isinstance(raw_plan, str):
                remediation_plan_serializable = [raw_plan]

        # Extract verification result
        raw_verification = current_execution_state.get("verification_result")
        verification_serializable = None
        if raw_verification:
            if hasattr(raw_verification, "model_dump"):
                verification_serializable = raw_verification.model_dump()
            elif hasattr(raw_verification, "dict"):
                verification_serializable = raw_verification.dict()

        # Store completed investigation in Qdrant only after objective verification.
        try:
            from .memory_store import get_memory_store
            from .verified_learning import (
                assess_learning_eligibility,
                build_provenance,
                memory_metadata_for_promotion,
            )

            act_report = (current_execution_state.get("metadata") or {}).get("act_report")
            verification_outcome = (act_report or {}).get(
                "verification"
            ) or verification_serializable
            eligibility = assess_learning_eligibility(
                act_report=act_report,
                verification_outcome=verification_outcome,
                live_results=(act_report or {}).get("live_results"),
                executed=(act_report or {}).get("executed"),
            )
            if eligibility.eligible_for_success:
                memory = get_memory_store()
                if memory.is_available():
                    provenance = build_provenance(
                        incident_id=str(incident_id),
                        eligibility=eligibility,
                        artifact_kind="memory",
                    )
                    memory.store_incident(
                        incident_text=f"Alert: {alert_name}\n\nResolution: {final_response}",
                        incident_id=str(incident_id),
                        metadata=memory_metadata_for_promotion(
                            eligibility=eligibility,
                            provenance=provenance,
                            extra={
                                "alert_name": alert_name,
                                "cluster_id": str(cluster_id),
                                "resolution": final_response,
                                "resolved_at": datetime.now(timezone.utc).isoformat(),
                            },
                        ),
                    )
            else:
                logger.info(
                    "Skipping successful-memory promotion for %s (%s)",
                    incident_id,
                    eligibility.outcome_class,
                )
        except Exception as me:
            logger.warning(f"Failed to store incident in memory: {me}")

        async with database.AsyncSessionLocal() as db:
            await db.execute(
                models.Incident.__table__
                .update()
                .where(models.Incident.id == incident_id)
                .values(
                    status=IncidentStatus.RESOLVED,
                    summary=final_response,
                    resolved_at=datetime.now(timezone.utc)
                )
            )

            if job_id:
                await db.execute(
                    models.Job.__table__
                    .update()
                    .where(models.Job.id == job_id)
                    .values(
                        status=JobStatus.COMPLETED,
                        completed_at=datetime.now(timezone.utc),
                        result=json.dumps({
                            "summary": final_response,
                            "hypothesis": final_response.split(".")[0] if final_response else "Issue identified.",
                            "plan": remediation_plan_serializable,
                            "actions": remediation_plan_serializable,
                            "verification": verification_serializable
                        })
                    )
                )
            await db.commit()

        # Push a lifecycle event so the incidents list / overview reflect the
        # resolution live, without waiting for their next poll.
        try:
            from .live_events import publish_lifecycle_event

            org_id = None
            if cluster_obj is not None and getattr(cluster_obj, "org_id", None):
                org_id = str(cluster_obj.org_id)
            await publish_lifecycle_event(
                "resolved",
                incident_id=str(incident_id),
                alert_name=alert_name,
                summary=final_response,
                org_id=org_id,
                status="resolved",
            )
        except Exception as _bus_err:
            logger.debug(f"incident-resolved publish skipped: {_bus_err}")

    except Exception as e:
        logger.error(f"SaaS Background execution failed: {e}")
        async with database.AsyncSessionLocal() as db:
             await db.execute(
                models.Incident.__table__
                .update()
                .where(models.Incident.id == incident_id)
                .values(status=IncidentStatus.OPEN, summary=f"Investigation failed: {str(e)}")
            )
             if job_id:
                 await db.execute(
                     models.Job.__table__.update()
                     .where(models.Job.id == job_id)
                     .values(status=JobStatus.FAILED, result=json.dumps({"error": str(e)}))
                 )
             await db.commit()

import os # Required for getenv in initial_state

