import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from backend import models, schemas, auth
import uuid

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(select(models.User).filter(models.User.email == email))
    return result.scalars().first()

async def create_org(db: AsyncSession, org: schemas.OrgCreate):
    # Generate API Key
    api_key = f"org_{uuid.uuid4().hex}"
    db_org = models.Organization(name=org.name, api_key=api_key)
    db.add(db_org)
    await db.commit()
    await db.refresh(db_org)
    return db_org

async def get_org_by_name(db: AsyncSession, name: str):
    result = await db.execute(select(models.Organization).filter(models.Organization.name == name))
    return result.scalars().first()


async def get_org_by_id(db: AsyncSession, org_id: uuid.UUID):
    result = await db.execute(select(models.Organization).filter(models.Organization.id == org_id))
    return result.scalars().first()

async def create_user(db: AsyncSession, user: schemas.UserCreate):
    # Check if org already exists — reuse if found, otherwise create new
    db_org = await get_org_by_name(db, user.org_name)
    if not db_org:
        org_data = schemas.OrgCreate(name=user.org_name)
        db_org = await create_org(db, org_data)

    # Check for duplicate email
    existing = await get_user_by_email(db, user.email)
    if existing:
        raise ValueError(f"User with email {user.email} already exists")

    hashed_password = auth.get_password_hash(user.password)

    # First user in org becomes ADMIN, subsequent users are MEMBER
    result = await db.execute(select(models.User).filter(models.User.org_id == db_org.id).limit(1))
    is_first_user = result.scalars().first() is None

    db_user = models.User(
        email=user.email,
        hashed_password=hashed_password,
        full_name=user.full_name,
        role=models.UserRole.ADMIN if is_first_user else models.UserRole.MEMBER,
        org_id=db_org.id
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user

async def get_clusters_for_org(db: AsyncSession, org_id: uuid.UUID):
    result = await db.execute(select(models.Cluster).filter(models.Cluster.org_id == org_id))
    return result.scalars().all()

async def create_cluster(db: AsyncSession, cluster: schemas.ClusterCreate, org_id: uuid.UUID):
    cluster_token = f"cl_{uuid.uuid4().hex}"
    db_cluster = models.Cluster(
        name=cluster.name,
        org_id=org_id,
        token=cluster_token,
        status=models.ClusterStatus.ONLINE,
        prometheus_url=cluster.prometheus_url,
        loki_url=cluster.loki_url,
        k8s_api_server=cluster.k8s_api_server,
        k8s_token=cluster.k8s_token,
        github_token=cluster.github_token,
        github_repo=cluster.github_repo,
        notion_api_key=cluster.notion_api_key,
        notion_database_id=cluster.notion_database_id,
    )
    db.add(db_cluster)
    await db.commit()
    await db.refresh(db_cluster)
    return db_cluster, cluster_token

async def get_cluster_by_token(db: AsyncSession, token: str):
    result = await db.execute(select(models.Cluster).filter(models.Cluster.token == token))
    return result.scalars().first()

async def get_cluster_by_id(db: AsyncSession, cluster_id: uuid.UUID):
    result = await db.execute(select(models.Cluster).filter(models.Cluster.id == cluster_id))
    return result.scalars().first()

async def update_cluster_heartbeat(db: AsyncSession, cluster_id: uuid.UUID):
    stmt = (
        models.Cluster.__table__
        .update()
        .where(models.Cluster.id == cluster_id)
        .values(
            last_heartbeat=datetime.now(timezone.utc),
            status=models.ClusterStatus.ONLINE
        )
    )
    await db.execute(stmt)
    await db.commit()

async def find_duplicate_incident(
    db: AsyncSession,
    cluster_id: uuid.UUID,
    title: str,
    window_minutes: int = 30
) -> Optional[models.Incident]:
    """Check for existing open/investigating incident with same title within time window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    result = await db.execute(
        select(models.Incident)
        .filter(
            models.Incident.cluster_id == cluster_id,
            models.Incident.title == title,
            models.Incident.status.in_([models.IncidentStatus.OPEN, models.IncidentStatus.INVESTIGATING]),
            models.Incident.created_at >= cutoff
        )
        .order_by(models.Incident.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def create_incident(db: AsyncSession, incident: schemas.IncidentCreate, cluster_id: uuid.UUID):
    db_incident = models.Incident(
        title=incident.title,
        description=incident.description,
        severity=incident.severity,
        cluster_id=cluster_id
    )
    db.add(db_incident)
    await db.commit()
    await db.refresh(db_incident)
    return db_incident


def _serialize_timeline_payload(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, default=str)


async def _get_next_timeline_sequence(db: AsyncSession, incident_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(models.IncidentTimelineEvent.sequence), 0)).where(
            models.IncidentTimelineEvent.incident_id == incident_id
        )
    )
    return int(result.scalar_one()) + 1


async def create_incident_timeline_event(
    db: AsyncSession,
    incident_id: uuid.UUID,
    event_type: str,
    speaker_role: str,
    content: str,
    title: Optional[str] = None,
    payload: Optional[Any] = None,
    pending_supervisor: bool = False,
    handled_at: Optional[datetime] = None,
) -> models.IncidentTimelineEvent:
    sequence = await _get_next_timeline_sequence(db, incident_id)
    db_event = models.IncidentTimelineEvent(
        incident_id=incident_id,
        sequence=sequence,
        event_type=event_type,
        speaker_role=speaker_role,
        title=title,
        content=content,
        payload_json=_serialize_timeline_payload(payload),
        pending_supervisor=pending_supervisor,
        handled_at=handled_at,
    )
    db.add(db_event)

    if event_type == "summary":
        incident = await db.get(models.Incident, incident_id)
        if incident is not None:
            incident.summary = content

    await db.commit()
    await db.refresh(db_event)
    return db_event


async def get_incident_timeline_events(db: AsyncSession, incident_id: uuid.UUID) -> List[models.IncidentTimelineEvent]:
    result = await db.execute(
        select(models.IncidentTimelineEvent)
        .filter(models.IncidentTimelineEvent.incident_id == incident_id)
        .order_by(models.IncidentTimelineEvent.sequence.asc(), models.IncidentTimelineEvent.created_at.asc())
    )
    return result.scalars().all()


async def get_pending_human_timeline_events(
    db: AsyncSession,
    incident_id: uuid.UUID,
    limit: int = 1,
) -> List[models.IncidentTimelineEvent]:
    result = await db.execute(
        select(models.IncidentTimelineEvent)
        .filter(
            models.IncidentTimelineEvent.incident_id == incident_id,
            models.IncidentTimelineEvent.event_type == "human_message",
            models.IncidentTimelineEvent.pending_supervisor.is_(True),
        )
        .order_by(models.IncidentTimelineEvent.sequence.asc())
        .limit(limit)
    )
    return result.scalars().all()


async def mark_incident_timeline_event_handled(
    db: AsyncSession,
    event_id: uuid.UUID,
    handled_at: Optional[datetime] = None,
) -> None:
    event = await db.get(models.IncidentTimelineEvent, event_id)
    if event is None:
        return

    event.pending_supervisor = False
    event.handled_at = handled_at or datetime.now(timezone.utc)
    await db.commit()

async def get_incidents_for_cluster(db: AsyncSession, cluster_id: uuid.UUID):
    result = await db.execute(select(models.Incident).filter(models.Incident.cluster_id == cluster_id).order_by(models.Incident.created_at.desc()))
    return result.scalars().all()

async def delete_cluster(db: AsyncSession, cluster_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    result = await db.execute(select(models.Cluster).filter(models.Cluster.id == cluster_id, models.Cluster.org_id == org_id))
    cluster = result.scalars().first()
    if cluster:
        await db.delete(cluster)
        await db.commit()
        return True
    return False

# ----------------------------------------------------------------------
# Job CRUD
# ----------------------------------------------------------------------

async def create_job(db: AsyncSession, cluster_id: uuid.UUID, job: schemas.JobCreate) -> models.Job:
    db_job = models.Job(
        cluster_id=cluster_id,
        job_type=job.job_type,
        payload=job.payload,
        status=models.JobStatus.PENDING
    )
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)
    return db_job

async def get_pending_job_for_cluster(db: AsyncSession, cluster_id: uuid.UUID) -> Optional[models.Job]:
    result = await db.execute(
        select(models.Job)
        .filter(
            models.Job.cluster_id == cluster_id,
            models.Job.status == models.JobStatus.PENDING,
            models.Job.job_type == models.JobType.TOOL_CALL
        )
        .order_by(models.Job.created_at.asc())
        .limit(1)
    )
    return result.scalars().first()

async def get_job_by_id(db: AsyncSession, job_id: uuid.UUID) -> Optional[models.Job]:
    result = await db.execute(select(models.Job).filter(models.Job.id == job_id))
    return result.scalars().first()

async def update_job_status(db: AsyncSession, job_id: uuid.UUID, status_update: schemas.JobStatusUpdate) -> Optional[models.Job]:
    job = await get_job_by_id(db, job_id)
    if not job:
        return None
    
    job.status = status_update.status
    if status_update.result:
        job.result = status_update.result
    if status_update.logs:
        # Append logs if existing
        job.logs = (job.logs or "") + status_update.logs
    
    if status_update.status == models.JobStatus.RUNNING and not job.started_at:
        job.started_at = datetime.now(timezone.utc)
    elif status_update.status in (models.JobStatus.COMPLETED, models.JobStatus.FAILED):
        job.completed_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(job)
    return job


async def get_jobs_for_cluster(db: AsyncSession, cluster_id: uuid.UUID):
    """Get all jobs for a cluster, ordered by most recent first."""
    result = await db.execute(
        select(models.Job)
        .filter(models.Job.cluster_id == cluster_id)
        .order_by(models.Job.created_at.desc())
    )
    return result.scalars().all()


async def create_audit_event(
    db: AsyncSession, 
    cluster_id: uuid.UUID, 
    action_type: str, 
    resource_target: str, 
    outcome: str, 
    actor_type: str = "AGENT",
    actor_id: str = "sre-agent",
    details: str = None
):
    """Log an immutable audit event."""
    audit_event = models.AuditEvent(
        cluster_id=cluster_id,
        action_type=action_type,
        resource_target=resource_target,
        outcome=outcome,
        actor_type=actor_type,
        actor_id=actor_id,
        details=details
    )
    db.add(audit_event)
    await db.commit()
    await db.refresh(audit_event)
    return audit_event

async def get_audit_events(db: AsyncSession, cluster_id: uuid.UUID, limit: int = 50):
    """Retrieve audit trail for a cluster."""
    result = await db.execute(
        select(models.AuditEvent)
        .filter(models.AuditEvent.cluster_id == cluster_id)
        .order_by(models.AuditEvent.timestamp.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ----------------------------------------------------------------------
# SLO CRUD
# ----------------------------------------------------------------------

async def create_slo(db: AsyncSession, cluster_id: uuid.UUID, slo: schemas.SLOCreate) -> models.SLO:
    db_slo = models.SLO(
        cluster_id=cluster_id,
        name=slo.name,
        sli_metric=slo.sli_metric,
        target=slo.target,
        window_days=slo.window_days
    )
    db.add(db_slo)
    await db.commit()
    await db.refresh(db_slo)
    return db_slo

async def get_slos_for_cluster(db: AsyncSession, cluster_id: uuid.UUID) -> List[models.SLO]:
    result = await db.execute(
        select(models.SLO).filter(models.SLO.cluster_id == cluster_id)
    )
    return result.scalars().all()

async def get_slo_by_id(db: AsyncSession, slo_id: uuid.UUID) -> Optional[models.SLO]:
    result = await db.execute(select(models.SLO).filter(models.SLO.id == slo_id))
    return result.scalars().first()

async def update_slo_metrics(
    db: AsyncSession, slo_id: uuid.UUID,
    current_value: float, error_budget_remaining: float
) -> Optional[models.SLO]:
    slo = await get_slo_by_id(db, slo_id)
    if not slo:
        return None
    slo.current_value = current_value
    slo.error_budget_remaining = error_budget_remaining
    slo.last_calculated = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(slo)
    return slo

async def delete_slo(db: AsyncSession, slo_id: uuid.UUID) -> bool:
    slo = await get_slo_by_id(db, slo_id)
    if not slo:
        return False
    await db.delete(slo)
    await db.commit()
    return True
