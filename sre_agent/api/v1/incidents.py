import logging
import json
from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select

from backend import schemas, crud, models, database
from backend.rbac import require_admin
from sre_agent.api.v1.clusters import get_current_user_and_org

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/clusters/{cluster_id}",
    tags=["incidents"],
)

@router.get("/incidents", response_model=List[schemas.IncidentResponse])
async def list_incidents(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """List incidents for a cluster."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
        
    return await crud.get_incidents_for_cluster(db, cluster_id)

@router.post("/trigger", response_model=schemas.IncidentResponse)
async def trigger_incident(
    cluster_id: uuid.UUID,
    payload: schemas.IncidentCreate,
    background_tasks: BackgroundTasks,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Manually trigger the SRE Agent for a cluster."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    # Check for duplicate incident
    existing = await crud.find_duplicate_incident(db, cluster_id, payload.title)
    if existing:
        logger.info(f"Dedup: incident '{payload.title}' already open as {existing.id}")
        return existing

    # 1. Create Incident Record
    incident = await crud.create_incident(db, payload, cluster_id)

    # 2. Create a shadow Job record so the Dashboard "Incident Command Center" can see it
    # This bridges the gap between the LangGraph-based Incident flow and the Job-based UI.
    from backend.models import JobType, JobStatus
    shadow_job = await crud.create_job(
        db=db,
        cluster_id=cluster_id,
        job=schemas.JobCreate(
            job_type=JobType.INVESTIGATION,
            payload=json.dumps({
                "incident_id": str(incident.id),
                "alert": payload.title,
                "severity": payload.severity.value if hasattr(payload.severity, 'value') else str(payload.severity)
            })
        )
    )

    # 3. Trigger Background Agent
    # We delay the import to avoid top-level circular dependency if any
    try:
        from sre_agent.agent_runtime import run_graph_background_saas
        # We need a new function that handles postgres logging
        background_tasks.add_task(
            run_graph_background_saas, 
            incident_id=incident.id, 
            cluster_id=cluster.id,
            alert_name=payload.title,
            job_id=shadow_job.id  # Pass the job ID to the background task
        )
    except ImportError as e:
        logger.error(
            f"Failed to import run_graph_background_saas: {e}. "
            f"Incident {incident.id} created but no investigation will run."
        )

    return incident


@router.delete("/incidents", status_code=200)
async def clear_cluster_incidents(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Delete all incidents for a cluster. Admin only."""
    require_admin(user)

    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    incidents = await crud.get_incidents_for_cluster(db, cluster_id)
    incident_ids = [incident.id for incident in incidents]
    incident_ids_str = {str(id) for id in incident_ids}

    if not incident_ids:
        return {"deleted": 0}

    # Get jobs related to these incidents (by parsing payload JSON)
    all_jobs = await db.execute(select(models.Job).where(models.Job.cluster_id == cluster_id))
    job_ids_to_delete = []
    
    for job in all_jobs.scalars().all():
        try:
            if job.payload:
                payload = json.loads(job.payload)
                if payload.get("incident_id") in incident_ids_str:
                    job_ids_to_delete.append(job.id)
        except (json.JSONDecodeError, TypeError):
            # If payload isn't valid JSON, log but continue
            logger.warning(f"Failed to parse job {job.id} payload: {job.payload}")

    # Bulk deletes do not trigger ORM cascades, so remove child rows explicitly first.
    await db.execute(
        delete(models.IncidentTimelineEvent).where(models.IncidentTimelineEvent.incident_id.in_(incident_ids))
    )
    if job_ids_to_delete:
        await db.execute(delete(models.Job).where(models.Job.id.in_(job_ids_to_delete)))
    await db.execute(delete(models.Incident).where(models.Incident.id.in_(incident_ids)))
    await db.commit()

    logger.info(f"Flushed {len(incident_ids)} incidents and {len(job_ids_to_delete)} related jobs for cluster {cluster_id}")

    from sre_agent.redis_state_store import get_state_store
    store = get_state_store()
    for incident_id in incident_ids:
        try:
            store.delete(str(incident_id))
        except Exception as e:
            logger.warning(f"Failed to delete incident {incident_id} from Redis: {e}")

    return {"deleted": len(incident_ids), "jobs_deleted": len(job_ids_to_delete)}
