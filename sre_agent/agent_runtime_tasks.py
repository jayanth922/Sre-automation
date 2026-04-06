import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .agent_state import AgentState
from .constants import SREConstants
from .callbacks import RedisLogCallbackHandler
from .redis_state_store import get_state_store
from backend import models, database
from backend.models import IncidentStatus, JobStatus
from sqlalchemy import func

logger = logging.getLogger(__name__)

async def run_graph_background_saas(
    incident_id: uuid.UUID,
    cluster_id: uuid.UUID,
    alert_name: str,
    job_id: Optional[uuid.UUID] = None
):
    """
    SaaS-aware background execution.
    Writes logs/results to the Postgres Database instead of just Redis.
    """
    from .agent_runtime import agent_graph, tools, initialize_agent
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
        # Ensure Agent System is initialized
        await initialize_agent()
        
        from langchain_core.messages import HumanMessage
        llm_provider = os.getenv("LLM_PROVIDER", "groq")
        
        initial_state: AgentState = {
            "messages": [HumanMessage(content=f"Investigate alert: {alert_name}")],
            "ooda_phase": "OBSERVE",
            "next": "investigation_swarm",
            "agent_results": {},
            "current_query": f"Investigate alert: {alert_name}",
            "metadata": {
                "llm_provider": llm_provider,
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
        
        async for event in agent_graph.astream(
            initial_state, 
            config={"callbacks": [callback_handler]}
        ):
            for node_name, node_output in event.items():
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_line = f"[{timestamp}] 🤖 AGENT_{node_name.upper()}: Step execution started."
                
                if node_name == "investigation_swarm":
                    log_line = f"[{timestamp}] 🔍 INVESTIGATION: Querying K8s, Metrics, and Logs in parallel..."
                elif node_name == "reflector":
                    log_line = f"[{timestamp}] 🧠 REFLECTOR: Correlating findings and forming hypothesis..."
                elif node_name == "policy_gate":
                    log_line = f"[{timestamp}] 🛡️ POLICY: Checking remediation safety rules..."
                
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
                
                current_execution_state = {**current_execution_state, **node_output}

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
