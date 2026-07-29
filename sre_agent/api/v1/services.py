"""Per-service health (RED signals) for a cluster, straight from Prometheus.

Groups the demo app's `service`-labelled metrics into a real service table:
request rate, error %, p95/p99 latency. No synthetic data — services with no
Prometheus samples simply don't appear.
"""
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(prefix="/clusters", tags=["services"])


async def _query(client: httpx.AsyncClient, base: str, promql: str) -> List[Dict[str, Any]]:
    try:
        resp = await client.get(f"{base}/api/v1/query", params={"query": promql})
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
    except Exception:
        pass
    return []


def _by_service(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in rows:
        svc = r.get("metric", {}).get("service")
        if not svc:
            continue
        try:
            out[svc] = float(r["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _status(error_pct: Optional[float], p95_ms: Optional[float]) -> str:
    e = error_pct or 0.0
    p = p95_ms or 0.0
    if e >= 5 or p >= 1000:
        return "crit"
    if e >= 1 or p >= 500:
        return "warn"
    return "ok"


@router.get("/{cluster_id}/services")
async def get_cluster_services(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> List[Dict[str, Any]]:
    """Per-service golden signals (RED) grouped by the Prometheus `service` label."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    base = (cluster.prometheus_url or os.getenv("PROMETHEUS_URL") or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="No Prometheus endpoint configured for this cluster.",
        )

    q_rps = "sum by (service) (rate(http_requests_total[1m]))"
    q_total = "sum by (service) (rate(http_requests_total[5m]))"
    q_5xx = 'sum by (service) (rate(http_requests_total{status=~"5.."}[5m]))'
    q_p95 = "histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m]))) * 1000"
    q_p99 = "histogram_quantile(0.99, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m]))) * 1000"

    async with httpx.AsyncClient(timeout=6.0) as client:
        rps = _by_service(await _query(client, base, q_rps))
        total = _by_service(await _query(client, base, q_total))
        five = _by_service(await _query(client, base, q_5xx))
        p95 = _by_service(await _query(client, base, q_p95))
        p99 = _by_service(await _query(client, base, q_p99))

    names = sorted(set(rps) | set(total) | set(p95) | set(p99))
    services: List[Dict[str, Any]] = []
    for name in names:
        tot = total.get(name, 0.0)
        err_pct = round((five.get(name, 0.0) / tot * 100.0), 2) if tot > 0 else 0.0
        p95_ms = round(p95[name], 0) if name in p95 and p95[name] == p95[name] else None
        p99_ms = round(p99[name], 0) if name in p99 and p99[name] == p99[name] else None
        services.append(
            {
                "name": name,
                "workload": f"deploy/{name}",
                "status": _status(err_pct, p95_ms),
                "rps": round(rps.get(name, 0.0), 1),
                "error_pct": err_pct,
                "p95_ms": p95_ms,
                "p99_ms": p99_ms,
                "cpu_pct": None,
                "mem_pct": None,
            }
        )
    return services
