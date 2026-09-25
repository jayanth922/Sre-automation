"""Job Queue Router for Agent-SaaS Integration."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from backend import schemas, crud, models, database
from backend.auth import decode_access_token
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.run_manifest import compare_run_manifests
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

# Dependency: Get cluster by token (for Agent polling)
async def get_cluster_by_token(
    authorization: str = Header(...),
    db: AsyncSession = Depends(database.get_db)
) -> models.Cluster:
    """Extract cluster token from Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]  # Remove "Bearer "
    cluster = await crud.get_cluster_by_token(db, token)
    if not cluster:
        raise HTTPException(status_code=401, detail="Invalid cluster token")
    return cluster


router = APIRouter(
    prefix="/clusters",
    tags=["jobs"],
    dependencies=[Depends(get_current_user_and_org)],
)


# ====================================
# Dashboard Endpoints (User-triggered)
# ====================================

@router.post("/{cluster_id}/jobs/trigger", response_model=schemas.JobResponse)
async def trigger_job(
    cluster_id: uuid.UUID,
    job: schemas.JobCreate,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Trigger a new job for a cluster (called from Dashboard)."""
    # Verify cluster belongs to user's org
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    new_job = await crud.create_job(db, cluster_id, job)
    return new_job


@router.get("/{cluster_id}/jobs", response_model=list[schemas.JobResponse])
async def list_jobs(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """List all jobs for a cluster (called from Dashboard)."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    return await crud.get_jobs_for_cluster(db, cluster_id)


@router.get(
    "/{cluster_id}/jobs/{job_id}/manifest",
    response_model=schemas.RunManifestResponse,
)
async def get_job_manifest(
    cluster_id: uuid.UUID,
    job_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """Return immutable provenance for one incident job."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    manifest = await crud.get_run_manifest_for_job(db, cluster_id, job_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Run manifest not found")
    return manifest


@router.get(
    "/{cluster_id}/jobs/{job_id}/manifest/compare/{other_job_id}",
    response_model=schemas.RunManifestComparisonResponse,
)
async def compare_job_manifests(
    cluster_id: uuid.UUID,
    job_id: uuid.UUID,
    other_job_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """Show exact configuration and input drift between two tenant-owned runs."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    left = await crud.get_run_manifest_for_job(db, cluster_id, job_id)
    right = await crud.get_run_manifest_for_job(db, cluster_id, other_job_id)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Run manifest not found")
    return compare_run_manifests(left, right)


@router.post("/{cluster_id}/jobs/{job_id}/cancel", response_model=schemas.JobResponse)
async def cancel_job(
    cluster_id: uuid.UUID,
    job_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    """Request cancellation of a durable investigation job."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    job = await crud.get_job_by_id(db, job_id)
    if not job or job.cluster_id != cluster_id:
        raise HTTPException(status_code=404, detail="Job not found")

    from sre_agent.durable_jobs import DurableJobError
    from sre_agent.job_store import request_job_cancel

    try:
        record = await request_job_cancel(db, job_id)
    except DurableJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    refreshed = await crud.get_job_by_id(db, record.id)
    return refreshed

