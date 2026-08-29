import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from backend import auth, crypto, models, schemas
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
    """Register a founding admin in a new organization.

    Registration never joins an organization by a caller-supplied name. Joining
    an existing organization is exclusively handled by invitation acceptance.
    """
    existing = await get_user_by_email(db, user.email)
    if existing:
        raise ValueError(f"User with email {user.email} already exists")

    db_org = models.Organization(
        name=user.org_name,
        api_key=f"org_{uuid.uuid4().hex}",
    )
    db.add(db_org)
    await db.flush()

    hashed_password = auth.get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        hashed_password=hashed_password,
        full_name=user.full_name,
        role=models.UserRole.ADMIN,
        org_id=db_org.id
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user

async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID):
    result = await db.execute(select(models.User).filter(models.User.id == user_id))
    return result.scalars().first()


async def get_users_for_org(db: AsyncSession, org_id: uuid.UUID):
    """All users in an org, newest first."""
    result = await db.execute(
        select(models.User)
        .filter(models.User.org_id == org_id)
        .order_by(models.User.created_at.asc())
    )
    return result.scalars().all()


async def count_active_admins(db: AsyncSession, org_id: uuid.UUID) -> int:
    """How many active admins the org has — used to prevent lockout."""
    result = await db.execute(
        select(func.count())
        .select_from(models.User)
        .filter(
            models.User.org_id == org_id,
            models.User.role == models.UserRole.ADMIN,
            models.User.is_active == True,  # noqa: E712
        )
    )
    return int(result.scalar_one())


async def update_user_role(db: AsyncSession, user: models.User, role: models.UserRole):
    user.role = role
    await db.commit()
    await db.refresh(user)
    return user


async def set_user_active(db: AsyncSession, user: models.User, is_active: bool):
    user.is_active = is_active
    if not is_active:
        # Revoke all refresh sessions so a deactivated user is logged out.
        await revoke_all_user_refresh_sessions(db, user.id)
    await db.commit()
    await db.refresh(user)
    return user


async def get_clusters_for_org(db: AsyncSession, org_id: uuid.UUID):
    result = await db.execute(select(models.Cluster).filter(models.Cluster.org_id == org_id))
    return result.scalars().all()

async def create_cluster(db: AsyncSession, cluster: schemas.ClusterCreate, org_id: uuid.UUID):
    import json as _json

    cluster_token = f"cl_{uuid.uuid4().hex}"
    db_cluster = models.Cluster(
        name=cluster.name,
        org_id=org_id,
        token=cluster_token,
        token_hash=crypto.credential_lookup_hash(cluster_token),
        key_version=crypto.current_key_version(),
        execution_context_version=1,
        status=models.ClusterStatus.ONLINE,
        prometheus_url=cluster.prometheus_url,
        loki_url=cluster.loki_url,
        k8s_api_server=cluster.k8s_api_server,
        k8s_token=cluster.k8s_token,
        github_token=cluster.github_token,
        github_repo=cluster.github_repo,
        notion_api_key=cluster.notion_api_key,
        notion_database_id=cluster.notion_database_id,
        metrics_config=_json.dumps(cluster.metrics_config) if cluster.metrics_config else None,
        namespace=cluster.namespace or None,
        llm_provider=cluster.llm_provider or None,
        llm_model=cluster.llm_model or None,
        llm_base_url=cluster.llm_base_url or None,
        llm_api_key=cluster.llm_api_key or None,
    )
    from sre_agent.cluster_context import resolve_authorized_llm

    resolve_authorized_llm(db_cluster)
    db.add(db_cluster)
    await db.commit()
    await db.refresh(db_cluster)
    return db_cluster, cluster_token


async def update_cluster(db: AsyncSession, cluster_id: uuid.UUID, org_id: uuid.UUID, update: "schemas.ClusterUpdate"):
    import json as _json

    cluster = await get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != org_id:
        return None
    data = update.model_dump(exclude_unset=True)
    credential_fields = {
        "k8s_token",
        "github_token",
        "notion_api_key",
        "llm_api_key",
    }
    for field in (
        "name",
        "prometheus_url",
        "loki_url",
        "k8s_api_server",
        "k8s_token",
        "github_token",
        "github_repo",
        "notion_api_key",
        "notion_database_id",
    ):
        if field in data and data[field] is not None:
            setattr(cluster, field, data[field])
    # Scope + LLM override: an explicitly-sent empty string clears the field
    # (revert to whole-cluster / platform-default), so honor blanks here.
    for field in ("namespace", "llm_provider", "llm_model", "llm_base_url", "llm_api_key"):
        if field in data:
            setattr(cluster, field, data[field] or None)
    # Refuse configs the runtime would reject at investigation start.
    if any(field in data for field in ("llm_provider", "llm_model", "llm_base_url", "llm_api_key")):
        from sre_agent.cluster_context import resolve_authorized_llm

        resolve_authorized_llm(cluster)
    context_fields = credential_fields | {
        "prometheus_url",
        "loki_url",
        "k8s_api_server",
        "namespace",
        "llm_provider",
        "llm_model",
        "llm_base_url",
    }
    if credential_fields.intersection(data):
        cluster.key_version = crypto.current_key_version()
    if context_fields.intersection(data):
        cluster.execution_context_version = (cluster.execution_context_version or 0) + 1
    if "metrics_config" in data:
        cluster.metrics_config = _json.dumps(data["metrics_config"]) if data["metrics_config"] else None
    await db.commit()
    await db.refresh(cluster)
    return cluster

async def get_cluster_by_token(db: AsyncSession, token: str):
    token_hash = crypto.credential_lookup_hash(token)
    result = await db.execute(
        select(models.Cluster).filter(models.Cluster.token_hash == token_hash)
    )
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
    window_minutes: Optional[int] = None,
) -> Optional[models.Incident]:
    """Return an existing OPEN/INVESTIGATING incident with the same title, if any.

    Dedup collapses a re-firing alert into the incident already tracking it for as
    long as that incident stays open — not just within a short window — so a
    long-running condition never spawns duplicates. Resolved incidents don't
    match, so a genuinely new occurrence after resolution opens a fresh incident.
    (window_minutes is optional and only narrows the lookback if explicitly set.)
    """
    filters = [
        models.Incident.cluster_id == cluster_id,
        models.Incident.title == title,
        models.Incident.status.in_([models.IncidentStatus.OPEN, models.IncidentStatus.INVESTIGATING]),
    ]
    if window_minutes:
        filters.append(models.Incident.created_at >= datetime.now(timezone.utc) - timedelta(minutes=window_minutes))
    result = await db.execute(
        select(models.Incident).filter(*filters).order_by(models.Incident.created_at.desc()).limit(1)
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


# ── Refresh sessions ─────────────────────────────────────────────────────────
async def create_refresh_session(db: AsyncSession, user_id: uuid.UUID, token_hash: str, family_id: uuid.UUID, expires_at: datetime):
    session = models.RefreshSession(
        user_id=user_id, token_hash=token_hash, family_id=family_id, expires_at=expires_at
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_refresh_session_by_hash(db: AsyncSession, token_hash: str):
    result = await db.execute(
        select(models.RefreshSession).filter(models.RefreshSession.token_hash == token_hash)
    )
    return result.scalars().first()


async def revoke_refresh_session(db: AsyncSession, session: "models.RefreshSession") -> None:
    session.revoked = True
    await db.commit()


async def revoke_all_user_refresh_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every active refresh session for a user (no commit — caller commits)."""
    result = await db.execute(
        select(models.RefreshSession).filter(
            models.RefreshSession.user_id == user_id,
            models.RefreshSession.revoked == False,  # noqa: E712
        )
    )
    for s in result.scalars().all():
        s.revoked = True


async def revoke_refresh_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    result = await db.execute(
        select(models.RefreshSession).filter(
            models.RefreshSession.family_id == family_id,
            models.RefreshSession.revoked == False,  # noqa: E712
        )
    )
    for s in result.scalars().all():
        s.revoked = True
    await db.commit()
