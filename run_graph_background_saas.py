
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
    
    logger.info(f"▶️ SaaS background graph execution DISABLED for testing. (Incident: {incident_id})")
    return # DISABLED FOR TESTING
    
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
            "next": "investigation_swarm",
            "agent_results": {},
            "current_query": f"Investigate alert: {alert_name}",
            "metadata": {
                "llm_provider": os.getenv("LLM_PROVIDER", "groq"),
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
                
                # Log entry
                log_line = f"[{datetime.now(timezone.utc).isoformat()}] Step completed: {node_name}"
                state_store.append_log(session_id, log_line)

                # Sync log to Job in Postgres if job_id is present
                if job_id:
                    try:
                        async with database.AsyncSessionLocal() as db:
                            # Fetch current logs to append
                            from sqlalchemy import select, update
                            job_res = await db.execute(select(models.Job).where(models.Job.id == job_id))
                            job_obj = job_res.scalar_one_or_none()
                            if job_obj:
                                current_logs = job_obj.logs or ""
                                new_logs = current_logs + log_line + "\n"
                                await db.execute(
                                    update(models.Job)
                                    .where(models.Job.id == job_id)
                                    .values(logs=new_logs)
                                )
                                await db.commit()
                    except Exception as le:
                        logger.warning(f"Failed to sync log to job: {le}")
                
                # Merge state
                current_execution_state = {**current_execution_state, **node_output}
                
                # Update Redis
                state_store.set(session_id, {
                    "status": "RUNNING",
                    "current_node": node_name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "state": current_execution_state
                }, ttl=3600)

        # Completion
        final_response = current_execution_state.get("final_response", "Investigation completed.")
        state_store.append_log(session_id, f"[{datetime.now(timezone.utc).isoformat()}] ✅ Investigation Complete")
        
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

        # Update Incident and Job in Postgres
        async with database.AsyncSessionLocal() as db:
            # Update Incident
            stmt_inc = (
                models.Incident.__table__
                .update()
                .where(models.Incident.id == incident_id)
                .values(
                    status=IncidentStatus.RESOLVED,
                    summary=final_response,
                    resolved_at=datetime.now(timezone.utc)
                )
            )
            await db.execute(stmt_inc)

            # Update Job
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
