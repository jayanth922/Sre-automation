import logging
import json
from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select

from backend import schemas, crud, models, database
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/clusters/{cluster_id}",
    tags=["incidents"],
    dependencies=[Depends(get_current_user_and_org)],
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

    # 2. Enqueue a durable leased investigation job (idempotent per incident).
    from sre_agent.job_worker import enqueue_and_kick

    parsed_labels: dict = {}
    parsed_annotations: dict = {}
    if payload.description:
        try:
            import re as _re
            match = _re.search(r"Labels:\s*(\{.*\})", payload.description, _re.DOTALL)
            if match:
                parsed_labels = json.loads(match.group(1))
            first_paragraph = payload.description.split("\n\n", 1)[0].strip()
            if first_paragraph:
                parsed_annotations["summary"] = first_paragraph
        except Exception as parse_err:
            logger.debug(f"Could not parse labels from description: {parse_err}")

    try:
        await enqueue_and_kick(
            db=db,
            cluster_id=cluster.id,
            organization_id=cluster.org_id,
            incident_id=incident.id,
            alert_name=payload.title,
            alert_labels=parsed_labels,
            alert_annotations=parsed_annotations,
            alert_starts_at=None,
            alert_severity=(
                payload.severity.value
                if hasattr(payload.severity, "value")
                else str(payload.severity)
            ),
            triggered_by="manual_trigger",
        )
    except Exception as e:
        logger.error(
            f"Failed to enqueue investigation for incident {incident.id}: {e}"
        )

    return incident


@router.delete("/incidents", status_code=200)
async def clear_cluster_incidents(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Delete all incidents for a cluster. Admin only."""
    await require_admin(user)

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
