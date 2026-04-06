"""Job Queue Router for Agent-SaaS Integration."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from backend import schemas, crud, models, database
from backend.auth import decode_access_token
from sre_agent.api.v1.auth_deps import get_current_user_and_org
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


