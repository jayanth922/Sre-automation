#!/usr/bin/env python3

import asyncio
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .agent_state import AgentState
from .constants import SREConstants

# Import logging config
from .logging_config import configure_logging
from .multi_agent_langgraph import create_multi_agent_system

# SaaS API Imports
from sre_agent.api.v1 import clusters, incidents
from backend import crud, database, models
from backend.routers import auth as auth_router
from backend.models import IncidentStatus, JobStatus
from sqlalchemy import func
import uuid

# Configure logging based on DEBUG environment variable
# This ensures debug mode works even when not run via __main__
if not logging.getLogger().handlers:
    # Check if DEBUG is already set in environment
    debug_from_env = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    configure_logging(debug_from_env)


# Custom filter to exclude /ping endpoint logs
class PingEndpointFilter(logging.Filter):
    def filter(self, record):
        # Filter out GET /ping requests from access logs
        if hasattr(record, "getMessage"):
            message = record.getMessage()
            if '"GET /ping HTTP/' in message:
                return False
        return True


# Configure uvicorn access logger to filter out ping requests
uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.addFilter(PingEndpointFilter())

logger = logging.getLogger(__name__)

# Simple FastAPI app
app = FastAPI(title="SRE Agent Runtime", version="1.0.0")

# Add CORS middleware — restrict origins in production via CORS_ORIGINS env var
_cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount SaaS API Routers
app.include_router(clusters.router, prefix="/api/v1")
app.include_router(incidents.router, prefix="/api/v1")
app.include_router(auth_router.router)

# Job Queue Router
from sre_agent.api.v1 import jobs as jobs_router
app.include_router(jobs_router.router, prefix="/api/v1")

# Mission Control Router (Audit Logs & Approvals)
from sre_agent.api.v1 import mission_control
app.include_router(mission_control.router, prefix="/api/v1")

# SLO Management Router
from sre_agent.api.v1 import slos as slos_router
app.include_router(slos_router.router, prefix="/api/v1")

# Alert Webhook Router (receives Alertmanager webhooks)
from sre_agent.api.v1 import alerts as alerts_router
app.include_router(alerts_router.router, prefix="/api/v1")

# Metrics Router (Golden Signals)
from sre_agent.api.v1 import metrics as metrics_router
app.include_router(metrics_router.router, prefix="/metrics")


# Simple request/response models
class InvocationRequest(BaseModel):
    input: Dict[str, Any]


class InvocationResponse(BaseModel):
    output: Dict[str, Any]


# Global variables for agent state
agent_graph = None
tools: list[BaseTool] = []

# Redis state store for pending approvals (replaces in-memory dict)
from .redis_state_store import get_state_store

state_store = get_state_store()


async def initialize_agent():
    """Initialize the SRE agent system using the same method as CLI."""
    global agent_graph, tools

    if agent_graph is not None:
        return  # Already initialized

    try:
        logger.info("Initializing SRE Agent system...")

        # Get provider from environment variable with ollama as default
        provider = os.getenv("LLM_PROVIDER", "ollama").lower()

        # Validate provider
        if provider not in ["groq", "ollama", "gemini"]:
            logger.warning(f"Invalid provider '{provider}', defaulting to 'ollama'")
            provider = "ollama"

        logger.info(f"Environment LLM_PROVIDER: {os.getenv('LLM_PROVIDER', 'NOT_SET')}")
        logger.info(f"Using LLM provider: {provider}")
        logger.info(f"Calling create_multi_agent_system with provider: {provider}")

        # Initialize persistence (MemorySaver for now, but could be Postgres)
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

        # Create multi-agent system using the same function as CLI
        agent_graph, tools = await create_multi_agent_system(provider, checkpointer=checkpointer)

        logger.info(
            f"SRE Agent system initialized successfully with {len(tools)} tools"
        )

    except Exception as e:
        from .llm_utils import LLMAccessError, LLMAuthenticationError, LLMProviderError

        if isinstance(e, (LLMAuthenticationError, LLMAccessError, LLMProviderError)):
            logger.error(f"LLM Provider Error: {e}")
            print(f"\n❌ {type(e).__name__}:")
            print(str(e))
            print("\n💡 Check your GROQ_API_KEY environment variable")
            print(f"   export LLM_PROVIDER=ollama")
        else:
            logger.error(f"Failed to initialize SRE Agent system: {e}")
        raise


# Global MCP client for metrics queries
mcp_client_global = None


async def get_mcp_client():
    """Get or create MCP client for metrics queries."""
    global mcp_client_global
    if mcp_client_global is None:
        from .multi_agent_langgraph import create_mcp_client
        mcp_client_global = create_mcp_client()
    return mcp_client_global


@app.on_event("startup")
async def startup_event():
    """Initialize agent on startup."""
    agent_mode = os.getenv("AGENT_MODE", "standalone").lower()
    cluster_token = os.getenv("CLUSTER_TOKEN", "")
    
    logger.info(f"Startup: AGENT_MODE={agent_mode}, HAS_TOKEN={bool(cluster_token)}")

    # Always initialize the AI graph if we are managing a cluster
    if agent_mode != "api" or cluster_token:
        logger.info("🧠 Initializing SRE Agent Graph for automated investigations...")
        await initialize_agent()
    else:
        logger.info("ℹ️ Running in Control Plane mode without local AI brain.")
    


@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: InvocationRequest):
    """Main agent invocation endpoint."""
    global agent_graph, tools

    logger.info("Received invocation request")

    try:
        # Ensure agent is initialized
        await initialize_agent()

        # Check if agent is enabled (It might be disabled in API Mode)
        if agent_graph is None:
            logger.info("Agent Graph is None (API Mode). Returning informational message.")
            return InvocationResponse(output={
                "message": "ℹ️ You are talking to the SaaS Control Plane. The Agent logic runs on your connected cluster. Please check the 'Active Investigations' or 'Logs' tab for real-time updates from your infrastructure.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": "ControlPlane",
            })

        # Extract user prompt
        user_prompt = request.input.get("prompt", "")
        if not user_prompt:
            raise HTTPException(
                status_code=400,
                detail="No prompt found in input. Please provide a 'prompt' key in the input.",
            )

        logger.info(f"Processing query: {user_prompt}")

        # Extract session_id and user_id from request
        session_id = request.input.get("session_id", "")
        user_id = request.input.get("user_id", "default_user")

        logger.info(f"Session ID: {session_id}, User ID: {user_id}")

        # Create initial state exactly like the CLI does
        initial_state: AgentState = {
            "messages": [HumanMessage(content=user_prompt)],
            "next": "supervisor",
            "agent_results": {},
            "current_query": user_prompt,
            "metadata": {
                "tools": tools,
            },
            "requires_collaboration": False,
            "agents_invoked": [],
            "final_response": None,
            "auto_approve_plan": True,  # Always auto-approve plans in runtime mode
            "session_id": session_id,  # Required for memory retrieval
            "user_id": user_id,  # Required for user personalization
        }

        # Process through the agent graph exactly like the CLI
        final_response = ""

        logger.info("Starting agent graph execution")

        async for event in agent_graph.astream(initial_state):
            for node_name, node_output in event.items():
                logger.info(f"Processing node: {node_name}")

                # Log key events from each node
                if node_name == "supervisor":
                    next_agent = node_output.get("next", "")
                    metadata = node_output.get("metadata", {})
                    logger.info(f"Supervisor routing to: {next_agent}")
                    if metadata.get("routing_reasoning"):
                        logger.info(
                            f"Routing reasoning: {metadata['routing_reasoning']}"
                        )

                elif node_name in [
                    "kubernetes_agent",
                    "logs_agent",
                    "metrics_agent",
                    "runbooks_agent",
                ]:
                    agent_results = node_output.get("agent_results", {})
                    logger.info(f"{node_name} completed with results")

                # Capture final response from aggregate node
                elif node_name == "aggregate":
                    final_response = node_output.get("final_response", "")
                    logger.info("Aggregate node completed, final response captured")

        if not final_response:
            logger.warning("No final response received from agent graph")
            final_response = (
                "I encountered an issue processing your request. Please try again."
            )
        else:
            logger.info(f"Final response length: {len(final_response)} characters")

        # Simple response format
        response_data = {
            "message": final_response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": SREConstants.app.agent_model_name,
        }

        logger.info("Successfully processed agent request")
        logger.info("Returning invocation response")
        return InvocationResponse(output=response_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent processing failed: {e}")
        logger.exception("Full exception details:")
        raise HTTPException(
            status_code=500, detail=f"Agent processing failed: {str(e)}"
        )


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Health check endpoint (GET/HEAD for Docker healthchecks)."""
    return {"status": "healthy"}





@app.get("/agent/state")
async def get_agent_state():
    """
    Get current agent state including thought traces and pending approvals.
    
    Returns:
        - Active investigations with thought traces
        - Pending approvals
        - Cluster health status
        - Active alerts count
    """
    try:
        pending_approvals = []
        active_investigations = []

        # Scan Redis for active sessions
        if state_store.is_available():
            try:
                keys = state_store.redis_client.keys("sre_agent:session:*")
                for key in keys:
                    session_id = key.decode().split(":")[-1] if isinstance(key, bytes) else key.split(":")[-1]
                    data = state_store.get(session_id)
                    if data:
                        if data.get("approval_required"):
                            pending_approvals.append({
                                "session_id": session_id,
                                "plan": data.get("remediation_plan"),
                                "status": data.get("status"),
                            })
                        if data.get("status") in ("RUNNING", "INVESTIGATING"):
                            active_investigations.append({
                                "session_id": session_id,
                                "current_node": data.get("current_node"),
                                "status": data.get("status"),
                            })
            except Exception as e:
                logger.warning(f"Error scanning Redis for sessions: {e}")

        # Query cluster health from database
        cluster_health = "unknown"
        active_alerts = 0
        try:
            from backend.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                from sqlalchemy.future import select
                result = await db.execute(select(models.Cluster))
                clusters = result.scalars().all()
                if clusters:
                    online = sum(1 for c in clusters if c.status == models.ClusterStatus.ONLINE)
                    total = len(clusters)
                    cluster_health = "healthy" if online == total else (
                        "degraded" if online > 0 else "offline"
                    )
                    # Count pending/running jobs as proxy for active alerts
                    from backend import crud
                    for cluster in clusters:
                        pending = await crud.get_pending_job_for_cluster(db, cluster.id)
                        if pending:
                            active_alerts += 1
                else:
                    cluster_health = "no_clusters"
        except Exception as e:
            logger.warning(f"Error querying cluster health: {e}")

        return {
            "pending_approvals": pending_approvals,
            "active_investigations": active_investigations,
            "cluster_health": cluster_health,
            "active_alerts": active_alerts,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Error getting agent state: {e}")
        return {
            "pending_approvals": [],
            "active_investigations": [],
            "cluster_health": "unknown",
            "active_alerts": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(e)
        }


@app.get("/agent/state/{session_id}")
async def get_agent_state_by_session(session_id: str):
    """
    Get live agent state, including running logs and approval status.
    """
    try:
        data = state_store.get(session_id)
        if not data:
            return {"status": "NOT_FOUND", "logs": []}
            
        # Fetch logs from atomic list
        logs = state_store.get_logs(session_id)
            
        return {
            "session_id": session_id,
            "status": data.get("status", "UNKNOWN"),
            "logs": logs,
            "current_node": data.get("current_node"),
            "approval_required": data.get("approval_required", False),
            "remediation_plan": data.get("remediation_plan"),
            "final_response": data.get("final_response"),
            "verification_result": data.get("verification_result"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error getting session state {session_id}: {e}")
        return {"status": "ERROR", "error": str(e)}


@app.post("/approve/{session_id}")
async def approve_remediation(session_id: str):
    """
    Human approval endpoint for remediation plans.
    
    Sets approval_status = APPROVED and resumes graph execution.
    
    Args:
        session_id: Session ID of the pending remediation
        
    Returns:
        Status of approval and resumed execution
    """
    global agent_graph

    logger.info(f"Received approval request for session: {session_id}")

    # Get pending state from Redis
    pending_data = state_store.get(session_id)
    if not pending_data:
        raise HTTPException(
            status_code=404,
            detail=f"No pending remediation found for session_id: {session_id}",
        )

    current_state = pending_data.get("state")

    if not current_state:
        raise HTTPException(
            status_code=400,
            detail="Invalid pending state - state data missing",
        )

    # Update approval status
    current_state["approval_status"] = "APPROVED"
    current_state["next"] = "aggregate"  # Resume at aggregate node

    logger.info(f"✅ Approval granted for session {session_id}, resuming execution")

    # Remove from Redis
    state_store.delete(session_id)

    # Resume graph execution from aggregate node
    from fastapi import BackgroundTasks
    # We can't easily spawn a background task from here without passing BackgroundTasks object
    # For now, we'll keep approval synchronous-ish but the graph execution helps
    # Ideally this would also be async converted
    try:
        # Ensure we have all required state fields
        from .agent_state import AgentState
        from langchain_core.messages import HumanMessage
        
        # Ensure messages exist
        if "messages" not in current_state or not current_state["messages"]:
            current_state["messages"] = [
                HumanMessage(content="Remediation plan approved, resuming execution")
            ]
        
        # Ensure metadata has tools
        if "metadata" not in current_state:
            current_state["metadata"] = {}
        if "tools" not in current_state["metadata"]:
            current_state["metadata"]["tools"] = tools
        
        final_response = ""
        execution_results = None
        verification_result = None
        
        async for event in agent_graph.astream(current_state):
            for node_name, node_output in event.items():
                logger.info(f"Resuming execution - Processing node: {node_name}")
                # ... (rest of logic) ...
                # Capture final response
                if node_name == "aggregate":
                    final_response = node_output.get("final_response", "")
                    logger.info("Resumed execution completed")

        return {
            "status": "approved",
            "message": "Remediation plan approved and execution completed",
            "final_response": final_response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to resume execution after approval: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to resume execution: {str(e)}",
        )


async def run_graph_background(
    session_id: str, 
    initial_state: Dict[str, Any], 
    alert_name: str
):
    """
    Background task to run the agent graph and update Redis state.
    """
    global agent_graph
    
    logger.info(f"▶️ Starting background graph execution for session: {session_id}")
    
    try:
        # Initial status update
        state_store.set(session_id, {
            "status": "RUNNING",
            # "logs" field removed
            "current_node": "start",
            "approval_required": False,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        current_execution_state = initial_state
        # Log initial message to Redis list
        state_store.append_log(session_id, f"[{datetime.now(timezone.utc).isoformat()}] Investigation started...")
        
        # Instantiate callback handler
        from .callbacks import RedisLogCallbackHandler
        callback_handler = RedisLogCallbackHandler(session_id)
        
        async for event in agent_graph.astream(
            initial_state, 
            config={"callbacks": [callback_handler]}
        ):
            for node_name, node_output in event.items():
                logger.info(f"Background processing node: {node_name}")
                
                # Add log entry
                log_entry = f"[{datetime.now(timezone.utc).isoformat()}] Step completed: {node_name}"
                state_store.append_log(session_id, log_entry)
                
                # Merge state
                current_execution_state = {**current_execution_state, **node_output}
                
                # Update Redis State (only structural state, not logs)
                update_data = {
                    "status": "RUNNING",
                    # "logs" field removed in favor of atomic list
                    "current_node": node_name,
                    "approval_required": False,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    # Store partial state in case of pause
                    "state": current_execution_state
                }

                state_store.set(session_id, update_data, ttl=3600)

        # Completion
        final_response = current_execution_state.get("final_response", "Investigation completed.")
        state_store.append_log(session_id, f"[{datetime.now(timezone.utc).isoformat()}] ✅ Investigation Complete")
        
        state_store.set(session_id, {
            "status": "COMPLETED",
            "current_node": "end",
            "approval_required": False,
            "final_response": final_response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verification_result": current_execution_state.get("verification_result")
        }, ttl=3600)
        
        logger.info(f"Background execution completed: {session_id}")

    except Exception as e:
        logger.error(f"Background execution failed: {e}")
        state_store.append_log(session_id, f"[{datetime.now(timezone.utc).isoformat()}] ❌ Error: {str(e)}")
        state_store.set(session_id, {
            "status": "ERROR",
            "error": str(e)
        })



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
    # Use incident ID as session ID for internal state
    session_id = str(incident_id)
    global agent_graph, tools
    
    logger.info(f"▶️ Starting SaaS background graph execution for incident: {incident_id} (Job: {job_id})")
    
    # Update Incident Status to INVESTIGATING and Job to RUNNING
    async with database.AsyncSessionLocal() as db:
        # Update Incident
        stmt_inc = (
            models.Incident.__table__
            .update()
            .where(models.Incident.id == incident_id)
            .values(status=IncidentStatus.INVESTIGATING)
        )
        await db.execute(stmt_inc)

        # Update Job if provided
        if job_id:
            from backend.models import JobStatus
            stmt_job = (
                models.Job.__table__
                .update()
                .where(models.Job.id == job_id)
                .values(
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(timezone.utc),
                    logs=f"[{datetime.now(timezone.utc).isoformat()}] Agent investigation started.\n"
                )
            )
            await db.execute(stmt_job)

        await db.commit()

    try:
        # Initialize Agent System if needed
        await initialize_agent()
        
        # Initialize State
        from .agent_state import AgentState
        from langchain_core.messages import HumanMessage
        
        initial_state: AgentState = {
            "messages": [HumanMessage(content=f"Investigate alert: {alert_name}")],
            "ooda_phase": "OBSERVE",
            "next": "supervisor",
            "agent_results": {},
            "current_query": f"Investigate alert: {alert_name}",
            "metadata": {
                "llm_provider": os.getenv("LLM_PROVIDER", "ollama"),
                "tools": tools,
                "cluster_id": str(cluster_id),
                "incident_id": str(incident_id),
            },
            "requires_collaboration": True,
            "agents_invoked": [],
            "final_response": None,
            "auto_approve_plan": True, # For automated SaaS flow, auto-approve for now
            "session_id": session_id,
            "user_id": "saas_user",
        }
        
        # Redis Logging Setup (for real-time UI updates)
        state_store.set(session_id, {
            "status": "RUNNING",
            "current_node": "start",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        start_log = f"[{datetime.now(timezone.utc).isoformat()}] Investigation started for Incident {incident_id}"
        state_store.append_log(session_id, start_log)
        
        from .callbacks import RedisLogCallbackHandler
        callback_handler = RedisLogCallbackHandler(session_id)
        
        current_execution_state = initial_state
        
        async for event in agent_graph.astream(
            initial_state, 
            config={"callbacks": [callback_handler]}
        ):
            for node_name, node_output in event.items():
                logger.info(f"SaaS Background processing node: {node_name}")
                
                # Format a clean log line for the UI
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log_line = f"[{timestamp}] 🤖 AGENT_{node_name.upper()}: Step execution started."
                
                if node_name == "investigation_swarm":
                    log_line = f"[{timestamp}] 🔍 INVESTIGATION: Querying K8s, Metrics, and Logs in parallel..."
                elif node_name == "reflector":
                    log_line = f"[{timestamp}] 🧠 REFLECTOR: Correlating findings and forming hypothesis..."
                
                # Push to Redis for potential low-latency UI needs
                state_store.append_log(session_id, log_line)

                # CRITICAL: Sync directly to the Job record in Postgres for the Dashboard Terminal
                if job_id:
                    try:
                        async with database.AsyncSessionLocal() as db:
                            from sqlalchemy import select, update, func
                            # Use func.concat to append logs atomically in the DB
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
                
                # Merge state
                current_execution_state = {**current_execution_state, **node_output}

        # Completion: Build the rich result object for the Dashboard cards
        final_response = current_execution_state.get("final_response", "Investigation completed.")
        
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

        # Update Incident and Job in Postgres with RICH DATA
        async with database.AsyncSessionLocal() as db:
            # Update Incident
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

            # Update Job with structured results for the Dashboard "Action Deck"
            if job_id:
                from backend.models import JobStatus
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
            
        logger.info(f"SaaS Background execution completed for incident: {incident_id}")

    except Exception as e:
        logger.error(f"SaaS Background execution failed: {e}")
        error_log = f"[{datetime.now(timezone.utc).isoformat()}] ❌ Error: {str(e)}"
        state_store.append_log(session_id, error_log)

        async with database.AsyncSessionLocal() as db:
            try:
                await crud.create_incident_timeline_event(
                    db,
                    incident_id,
                    event_type="system_event",
                    speaker_role="system",
                    title="System",
                    content=f"Investigation failed: {str(e)}",
                    payload={
                        "source": "runtime",
                        "job_id": str(job_id) if job_id else None,
                        "error": str(e),
                    },
                )
            except Exception as timeline_error:
                logger.warning(f"Failed to persist incident failure event: {timeline_error}")
        
        # Update Incident Status to OPEN (investigation failed) and Job to FAILED
        async with database.AsyncSessionLocal() as db:
             stmt_inc = (
                models.Incident.__table__
                .update()
                .where(models.Incident.id == incident_id)
                .values(
                    summary=f"Investigation Attempt Failed: {str(e)}",
                    status=IncidentStatus.OPEN
                )
            )
             await db.execute(stmt_inc)

             if job_id:
                 from backend.models import JobStatus
                 await db.execute(
                     models.Job.__table__
                     .update()
                     .where(models.Job.id == job_id)
                     .values(
                         status=JobStatus.FAILED,
                         completed_at=datetime.now(timezone.utc),
                         result=json.dumps({"error": str(e)})
                     )
                 )

             await db.commit()

@app.post("/webhook/alert", status_code=202)
async def webhook_alert(
    alert_payload: Dict[str, Any], 
    background_tasks: BackgroundTasks
):
    """
    Self-Defense Mode: Prometheus Alertmanager webhook endpoint.
    
    When Prometheus fires an alert:
    1. Create incident record in PostgreSQL (for SaaS Dashboard visibility)
    2. Start LangGraph investigation immediately
    3. Stream logs to SaaS via database updates
    
    Returns 202 Accepted immediately with incident_id.
    """
    global agent_graph, tools

    logger.info("🚨 [SELF-DEFENSE MODE] Received Prometheus alert webhook")

    try:
        # Ensure agent is initialized
        await initialize_agent()

        # Extract alert
        alerts = alert_payload.get("alerts", [])
        if not alerts:
            raise HTTPException(status_code=400, detail="No alerts found")
        
        alert = alerts[0]
        alert_name = alert.get("labels", {}).get("alertname", "UnknownAlert")
        severity_str = alert.get("labels", {}).get("severity", "warning")
        description = alert.get("annotations", {}).get("description", "")
        
        # Map severity string to enum
        from backend.models import IncidentSeverity
        severity_map = {
            "critical": IncidentSeverity.CRITICAL,
            "high": IncidentSeverity.HIGH,
            "warning": IncidentSeverity.MEDIUM,
            "low": IncidentSeverity.LOW,
        }
        severity = severity_map.get(severity_str.lower(), IncidentSeverity.MEDIUM)
        
        # Get cluster ID from CLUSTER_TOKEN environment variable
        cluster_token = os.getenv("CLUSTER_TOKEN", "")
        cluster_id = None
        
        if cluster_token:
            # Lookup cluster by token to get cluster_id
            async with database.AsyncSessionLocal() as db:
                cluster = await crud.get_cluster_by_token(db, cluster_token)
                if cluster:
                    cluster_id = cluster.id
                    logger.info(f"📡 Linked to cluster: {cluster.name} (ID: {cluster_id})")
        
        if not cluster_id:
            # Fallback: Run locally without SaaS tracking
            logger.warning("⚠️ No CLUSTER_TOKEN set - running in local mode (no SaaS visibility)")
            
            # Build context and run old-style background execution
            from .context_builder import ContextBuilder
            context_builder = ContextBuilder(tools)
            enriched_context = await context_builder.enrich_alert_context(alert)
            
            session_id = f"alert-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            incident_id_str = f"incident-{enriched_context.alert_name}-{session_id}"
            
            from .agent_state import AgentState, AlertContext
            llm_provider = os.getenv("LLM_PROVIDER", "ollama")
            
            initial_state: AgentState = {
                "messages": [HumanMessage(content=f"Alert: {enriched_context.alert_name}")],
                "ooda_phase": "OBSERVE",
                "alert_context": enriched_context,
                "next": "supervisor",
                "agent_results": {},
                "current_query": f"Investigate alert: {enriched_context.alert_name}",
                "metadata": {
                    "llm_provider": llm_provider,
                    "tools": tools,
                },
                "requires_collaboration": True,
                "agents_invoked": [],
                "final_response": None,
                "auto_approve_plan": False,
                "session_id": session_id,
                "user_id": "alertmanager",
                "incident_id": incident_id_str,
                "thought_traces": {},
            }
            
            background_tasks.add_task(
                run_graph_background, 
                session_id, 
                initial_state, 
                enriched_context.alert_name
            )
            
            return {
                "status": "accepted",
                "mode": "local",
                "session_id": session_id,
                "message": "Investigation started (local mode - no SaaS visibility)",
                "poll_url": f"/agent/state/{session_id}"
            }
        
        # =====================================================
        # SELF-DEFENSE MODE: Create Incident for SaaS Dashboard
        # =====================================================
        logger.info("🛡️ Creating incident record for SaaS Dashboard visibility...")
        
        async with database.AsyncSessionLocal() as db:
            # Check for duplicate incident before creating
            dedup_title = f"[AUTO] {alert_name}"
            existing = await crud.find_duplicate_incident(db, cluster_id, dedup_title)
            if existing:
                logger.info(f"Dedup: alert '{alert_name}' already tracked as incident {existing.id}")
                return {
                    "status": "deduplicated",
                    "mode": "self_defense",
                    "incident_id": str(existing.id),
                    "cluster_id": str(cluster_id),
                    "message": "Duplicate alert — existing incident already open",
                }

            # Create incident in PostgreSQL
            from backend import schemas
            incident_data = schemas.IncidentCreate(
                title=dedup_title,
                description=description or f"Automatically triggered by Prometheus alert: {alert_name}",
                severity=severity
            )
            incident = await crud.create_incident(db, incident_data, cluster_id)
            incident_id = incident.id
            logger.info(f"✅ Incident created: {incident_id}")
        
        # 🚀 Start SaaS-aware LangGraph execution
        background_tasks.add_task(
            run_graph_background_saas,
            incident_id=incident_id,
            cluster_id=cluster_id,
            alert_name=alert_name
        )
        
        logger.info(f"🚀 [SELF-DEFENSE MODE] Investigation launched for incident: {incident_id}")
        
        return {
            "status": "accepted",
            "mode": "self_defense",
            "incident_id": str(incident_id),
            "cluster_id": str(cluster_id),
            "message": "Self-Defense Mode activated - investigation started, SaaS Dashboard notified",
            "dashboard_url": f"/clusters/{cluster_id}/incidents"
        }

    except Exception as e:
        logger.error(f"Alert processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def invoke_sre_agent_async(prompt: str, provider: str = "ollama") -> str:
    """
    Programmatic interface to invoke SRE agent.

    Args:
        prompt: The user prompt/query
        provider: LLM provider (only "groq" is supported)

    Returns:
        The agent's response as a string
    """
    try:
        # Create the multi-agent system
        graph, tools = await create_multi_agent_system(provider=provider)

        # Create initial state
        initial_state: AgentState = {
            "messages": [HumanMessage(content=prompt)],
            "next": "supervisor",
            "agent_results": {},
            "current_query": prompt,
            "metadata": {},
            "requires_collaboration": False,
            "agents_invoked": [],
            "final_response": None,
        }

        # Execute and get final response
        final_response = ""
        async for event in graph.astream(initial_state):
            for node_name, node_output in event.items():
                if node_name == "aggregate":
                    final_response = node_output.get("final_response", "")

        return final_response or "I encountered an issue processing your request."

    except Exception as e:
        logger.error(f"Agent invocation failed: {e}")
        raise


def invoke_sre_agent(prompt: str, provider: str = "ollama") -> str:
    """
    Synchronous wrapper for invoke_sre_agent_async.

    Args:
        prompt: The user prompt/query
        provider: LLM provider (only "groq" is supported)

    Returns:
        The agent's response as a string
    """
    return asyncio.run(invoke_sre_agent_async(prompt, provider))


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="SRE Agent Runtime")
    parser.add_argument(
        "--provider",
        default=os.getenv("LLM_PROVIDER", "ollama"),
        help="LLM provider to use (default: ollama)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and trace output",
    )

    args = parser.parse_args()

    # Configure logging based on debug flag
    from .logging_config import configure_logging

    debug_enabled = configure_logging(args.debug)

    # Set environment variables
    os.environ["LLM_PROVIDER"] = args.provider
    os.environ["DEBUG"] = "true" if debug_enabled else "false"

    logger.info(f"Starting SRE Agent Runtime with provider: {args.provider}")
    if debug_enabled:
        logger.info("Debug logging enabled")
    uvicorn.run(app, host=args.host, port=args.port)
