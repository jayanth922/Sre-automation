from typing import List, Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, models, database
from backend.rbac import require_admin
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(
    prefix="/clusters",
    tags=["clusters"],
)

@router.post("", response_model=dict)
async def create_cluster(
    cluster: schemas.ClusterCreate,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Create a new cluster and return the connection token."""
    new_cluster, token = await crud.create_cluster(db, cluster, org_id=user.org_id)
    return {
        "id": str(new_cluster.id),
        "name": new_cluster.name,
        "token": token
    }

@router.get("", response_model=List[schemas.ClusterResponse])
async def list_clusters(
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """List all clusters for the user's organization."""
    return await crud.get_clusters_for_org(db, org_id=user.org_id)

@router.get("/{cluster_id}/health")
async def get_cluster_health(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Get latest health status of a cluster."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    
    # Ownership Check
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
        
    return {
        "status": cluster.status,
        "last_heartbeat": cluster.last_heartbeat
    }


@router.delete("/{cluster_id}", status_code=204)
async def delete_cluster(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Delete a cluster. Admin only."""
    require_admin(user)
    success = await crud.delete_cluster(db, cluster_id, user.org_id)
    if not success:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return


# ----------------------------------------------------------------------
# Break Glass & Audit API
# ----------------------------------------------------------------------

@router.get("/{cluster_id}/lock")
async def get_cluster_lock(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Check if cluster is locked."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    from sre_agent.redis_state_store import get_state_store
    storage = get_state_store()
    is_locked = storage.is_cluster_locked(str(cluster_id))
    return {"locked": is_locked}

@router.post("/{cluster_id}/lock")
async def set_cluster_lock(
    cluster_id: uuid.UUID,
    payload: dict,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Toggle Emergency Lock (Break Glass). Admin only."""
    require_admin(user)
    locked = payload.get("locked", False)
    
    from sre_agent.redis_state_store import get_state_store
    storage = get_state_store()
    success = storage.set_cluster_lock(str(cluster_id), locked)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update lock state")

    # Audit this action
    await crud.create_audit_event(
        db=db,
        cluster_id=cluster_id,
        action_type="EMERGENCY_LOCK_TOGGLE",
        resource_target="cluster",
        outcome="SUCCESS",
        actor_type="USER",
        actor_id=user.email,
        details=f"Lock set to {locked}"
    )
    
    return {"locked": locked}

@router.get("/{cluster_id}/audit")
async def get_cluster_audit_logs(
    cluster_id: uuid.UUID,
    limit: int = 50,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db)
):
    """Get audit trail for cluster."""
    events = await crud.get_audit_events(db, cluster_id, limit)
    return events
