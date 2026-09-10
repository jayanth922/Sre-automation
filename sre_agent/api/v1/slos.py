"""SLO Management API."""
import uuid
import logging
from typing import List, Optional

import httpx
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


async def _query_current_value(prometheus_url: str, promql: str) -> Optional[float]:
    """Evaluate an SLO's sli_metric as a raw PromQL instant query. Expected to
    resolve to a single value already expressed as a percentage (0-100),
    e.g. a success-rate ratio pre-multiplied by 100. Returns None (never
    raises) on any network/parse failure or an empty result, so a bad query
    or unreachable Prometheus degrades to the last persisted value instead
    of breaking the status endpoint."""
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{prometheus_url}/api/v1/query", params={"query": promql})
            data = resp.json()
            if data.get("status") == "success" and data["data"]["result"]:
                return float(data["data"]["result"][0]["value"][1])
    except Exception:
        logger.warning("slo_prometheus_query_failed", extra={"promql": promql})
    return None

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

@router.patch("/{slo_id}", response_model=schemas.SLOResponse)
async def update_slo_endpoint(
    cluster_id: uuid.UUID,
    slo_id: uuid.UUID,
    update: schemas.SLOUpdate,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
    owned_slo: models.SLO = Depends(get_owned_slo),
):
    """Edit an existing SLO's definition (name, SLI query, target, or window)."""
    slo = await crud.update_slo(db, slo_id, update)
    if not slo:
        raise HTTPException(status_code=404, detail="SLO not found")
    return slo

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
    owned_cluster: models.Cluster = Depends(get_owned_cluster),
):
    """Get SLO status with error budget and burn rate.

    Re-evaluates sli_metric against the cluster's own Prometheus on every
    call (the dashboard polls this every 20s), persisting the fresh value so
    it survives until the next successful query. Falls back to the last
    persisted current_value when Prometheus is unreachable, the query
    returns no series, or no prometheus_url is saved for this cluster.
    """
    live_value: Optional[float] = None
    if owned_cluster.prometheus_url:
        live_value = await _query_current_value(owned_cluster.prometheus_url, owned_slo.sli_metric)

    # Calculate error budget
    target = owned_slo.target / 100.0  # Convert 99.9 -> 0.999
    current_raw = live_value if live_value is not None else owned_slo.current_value
    current = (current_raw if current_raw is not None else 100.0) / 100.0
    total_budget = 1.0 - target  # e.g., 0.001 for 99.9%
    consumed = max(0.0, 1.0 - current) if total_budget > 0 else 0.0
    budget_consumed_pct = (consumed / total_budget * 100.0) if total_budget > 0 else 0.0
    budget_remaining_pct = max(0.0, 100.0 - min(budget_consumed_pct, 100.0))

    if live_value is not None:
        await crud.update_slo_metrics(db, slo_id, live_value, budget_remaining_pct)

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
