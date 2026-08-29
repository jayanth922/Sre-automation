from typing import List, Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, models, database
from sre_agent.api.v1.auth_deps import get_current_user_and_org, require_admin
from sre_agent.api.v1.ownership import get_owned_cluster

router = APIRouter(
    prefix="/clusters",
    tags=["clusters"],
    dependencies=[Depends(get_current_user_and_org)],
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

@router.patch("/{cluster_id}", response_model=schemas.ClusterResponse)
async def update_cluster_endpoint(
    cluster_id: uuid.UUID,
    update: schemas.ClusterUpdate,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Update a cluster's endpoints and observability config. Admin only."""
    await require_admin(user)
    cluster = await crud.update_cluster(db, cluster_id, user.org_id, update)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster

@router.get("/{cluster_id}/health")
async def get_cluster_health(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Get latest health status of a cluster from observed heartbeat evidence."""
    from sre_agent.cluster_heartbeat import heartbeat_payload

    return heartbeat_payload(
        owned_cluster.last_heartbeat,
        status=owned_cluster.status,
        source=getattr(owned_cluster, "heartbeat_source", None),
        reason=getattr(owned_cluster, "heartbeat_reason", None),
    )


@router.get("/{cluster_id}/incidents/awaiting-approval")
async def get_awaiting_approval(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Incidents whose remediation is paused waiting on human approval.

    Read-only: reuses the same graph-interrupt check as the per-incident status
    endpoint, so the rail can show a persistent 'needs approval' cue that survives
    reloads without any new state to maintain."""
    incidents = await crud.get_incidents_for_cluster(db, cluster_id)
    # Only non-resolved incidents can be paused; cap the scan to bound cost.
    open_incidents = [i for i in incidents if i.status != models.IncidentStatus.RESOLVED][:50]

    pending: list[str] = []
    try:
        from sre_agent.api.v1.mission_control import get_agent_graph
        graph = await get_agent_graph(cluster_id)
        for inc in open_incidents:
            try:
                st = await graph.aget_state({"configurable": {"thread_id": str(inc.id)}})
                if st and st.tasks and st.tasks[0].interrupts:
                    pending.append(str(inc.id))
            except Exception:
                continue
    except Exception:
        # Graph/checkpointer unavailable — report none rather than erroring.
        pending = []

    return {"incident_ids": pending, "count": len(pending)}


@router.delete("/{cluster_id}", status_code=204)
async def delete_cluster(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Delete a cluster. Admin only."""
    await require_admin(user)
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
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Check if cluster is locked."""
    from sre_agent.redis_state_store import get_state_store
    storage = get_state_store()
    is_locked = storage.is_cluster_locked(str(cluster_id))
    return {"locked": is_locked}

@router.post("/{cluster_id}/lock")
async def set_cluster_lock(
    cluster_id: uuid.UUID,
    payload: dict,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Toggle Emergency Lock (Break Glass). Admin only."""
    await require_admin(user)
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
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Get audit trail for cluster."""
    events = await crud.get_audit_events(db, cluster_id, limit)
    return events
