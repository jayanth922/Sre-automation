"""SLO Management API."""
import uuid
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, models, database
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.api.v1.ownership import get_owned_cluster, get_owned_slo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/clusters/{cluster_id}/slos",
    tags=["slos"],
    dependencies=[Depends(get_current_user_and_org)],
)

@router.post("", response_model=schemas.SLOResponse, status_code=201)
async def create_slo(
    cluster_id: uuid.UUID,
    slo: schemas.SLOCreate,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Define a new SLO for a cluster."""
    return await crud.create_slo(db, cluster_id, slo)

@router.get("", response_model=List[schemas.SLOResponse])
async def list_slos(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """List all SLOs for a cluster."""
    return await crud.get_slos_for_cluster(db, cluster_id)

@router.get("/{slo_id}/status", response_model=schemas.SLOStatusResponse)
async def get_slo_status(
    cluster_id: uuid.UUID,
    slo_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_slo: models.SLO = Depends(get_owned_slo),
):
    """Get SLO status with error budget and burn rate."""
    # Calculate error budget
    target = owned_slo.target / 100.0  # Convert 99.9 -> 0.999
    current = (owned_slo.current_value or 100.0) / 100.0
    total_budget = 1.0 - target  # e.g., 0.001 for 99.9%
    consumed = max(0.0, (1.0 - current) - 0) if total_budget > 0 else 0.0
    budget_consumed_pct = (consumed / total_budget * 100.0) if total_budget > 0 else 0.0

    return schemas.SLOStatusResponse(
        slo=schemas.SLOResponse.model_validate(owned_slo),
        budget_consumed_percent=min(budget_consumed_pct, 100.0),
        burn_rate_1h=None,  # Populated by Prometheus integration
        burn_rate_6h=None,
        is_breaching=budget_consumed_pct > 100.0
    )

@router.delete("/{slo_id}", status_code=204)
async def delete_slo_endpoint(
    cluster_id: uuid.UUID,
    slo_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_slo: models.SLO = Depends(get_owned_slo),
):
    """Delete an SLO."""
    success = await crud.delete_slo(db, slo_id)
    if not success:
        raise HTTPException(status_code=404, detail="SLO not found")
    return
