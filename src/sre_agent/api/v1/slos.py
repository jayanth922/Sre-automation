"""SLO Management API."""
import asyncio
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

async def _burn_rate(
    prometheus_url: str, sli_metric: str, target_fraction: float, window: str
) -> Optional[float]:
    """How many times faster than "exactly on budget" the SLO is burning.

    1.0 means the error budget runs out precisely at the end of the
    compliance window; 14.4 is the page-now figure in the standard
    multiwindow scheme, because it empties a 30-day budget in two days. This
    is the number that makes `budget_consumed_percent` actionable -- 40%
    consumed is fine at a burn rate of 1 and an emergency at 14.

    The window comes from a subquery, `avg_over_time((<sli_metric>)[1h:])`,
    rather than from rewriting a range inside the query: `sli_metric` is
    user-authored PromQL for the *current* success rate and may not contain a
    range at all. Returns None on the same terms as `_query_current_value` --
    an unreachable Prometheus, a query Prometheus rejects, or an empty result
    leaves the burn rate unavailable rather than wrong.
    """
    budget = 1.0 - target_fraction
    if budget <= 0:
        # A 100% target has no budget to burn. Both 0 and infinity would be
        # claims the data cannot support.
        return None
    average = await _query_current_value(
        prometheus_url, f"avg_over_time(({sli_metric})[{window}:])"
    )
    if average is None:
        return None
    # sli_metric is a percentage by the contract above, and a success rate
    # above 100 (or below 0) is a broken query, not a negative burn.
    error_rate = min(1.0, max(0.0, 1.0 - average / 100.0))
    return error_rate / budget


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

    # Two windows, not one: a burn rate over a single window cannot separate
    # "a short spike that is already over" from "still burning", which is the
    # distinction the on-call is actually making. Both are best-effort and
    # concurrent, so a slow Prometheus costs one round trip rather than two,
    # and a failing one leaves the field null exactly as before.
    burn_rate_1h: Optional[float] = None
    burn_rate_6h: Optional[float] = None
    if owned_cluster.prometheus_url:
        burn_rate_1h, burn_rate_6h = await asyncio.gather(
            _burn_rate(
                owned_cluster.prometheus_url, owned_slo.sli_metric, target, "1h"
            ),
            _burn_rate(
                owned_cluster.prometheus_url, owned_slo.sli_metric, target, "6h"
            ),
        )

    if live_value is not None:
        await crud.update_slo_metrics(db, slo_id, live_value, budget_remaining_pct)

    return schemas.SLOStatusResponse(
        slo=schemas.SLOResponse.model_validate(owned_slo),
        budget_consumed_percent=min(budget_consumed_pct, 100.0),
        burn_rate_1h=burn_rate_1h,
        burn_rate_6h=burn_rate_6h,
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
