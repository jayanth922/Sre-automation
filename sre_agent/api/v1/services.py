"""Per-service health (RED) and cluster golden signals — from Prometheus.

Queries are built from each cluster's resolved observability profile
(sre_agent.metrics_profile), so this works against any workload's metric schema,
not one demo's. Services with no samples simply don't appear; missing metrics
return nulls rather than synthetic data.
"""
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent import metrics_profile as mp

router = APIRouter(prefix="/clusters", tags=["services"])


async def _query_scalar(client: httpx.AsyncClient, base: str, promql: str) -> Optional[float]:
    try:
        resp = await client.get(f"{base}/api/v1/query", params={"query": promql})
        data = resp.json()
        if data.get("status") == "success" and data["data"]["result"]:
            return float(data["data"]["result"][0]["value"][1])
    except Exception:
        pass
    return None


async def _query_by_label(client: httpx.AsyncClient, base: str, promql: str, label: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        resp = await client.get(f"{base}/api/v1/query", params={"query": promql})
        data = resp.json()
        if data.get("status") == "success":
            for row in data["data"]["result"]:
                key = row.get("metric", {}).get(label)
                if not key:
                    continue
                try:
                    out[key] = float(row["value"][1])
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception:
        pass
    return out


def _prom_base(cluster: models.Cluster) -> str:
    import os

    return (cluster.prometheus_url or os.getenv("PROMETHEUS_URL") or "").rstrip("/")


async def _load_cluster(cluster_id: uuid.UUID, user: models.User, db: AsyncSession) -> models.Cluster:
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster


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
    """Per-service golden signals grouped by the cluster's configured service label."""
    cluster = await _load_cluster(cluster_id, user, db)
    base = _prom_base(cluster)
    if not base:
        raise HTTPException(status_code=503, detail="No Prometheus endpoint configured for this cluster.")

    cfg = mp.resolve(cluster.metrics_config)
    label = cfg["service_label"]

    async with httpx.AsyncClient(timeout=6.0) as client:
        rps = await _query_by_label(client, base, mp.q_service_rps(cfg), label)
        total = await _query_by_label(client, base, mp.q_service_total(cfg), label)
        five = await _query_by_label(client, base, mp.q_service_errors(cfg), label)
        p95 = await _query_by_label(client, base, mp.q_service_latency(cfg, 0.95), label)
        p99 = await _query_by_label(client, base, mp.q_service_latency(cfg, 0.99), label)

    names = sorted(set(rps) | set(total) | set(p95) | set(p99))
    services: List[Dict[str, Any]] = []
    for name in names:
        tot = total.get(name, 0.0)
        err_pct = round((five.get(name, 0.0) / tot * 100.0), 2) if tot > 0 else 0.0
        p95_ms = round(p95[name]) if name in p95 and p95[name] == p95[name] else None
        p99_ms = round(p99[name]) if name in p99 and p99[name] == p99[name] else None
        services.append(
            {
                "name": name,
                "workload": name,
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


@router.get("/{cluster_id}/metrics")
async def get_cluster_metrics(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Cluster-wide golden signals (error %, p95 latency, CPU, memory)."""
    cluster = await _load_cluster(cluster_id, user, db)
    base = _prom_base(cluster)
    if not base:
        raise HTTPException(status_code=503, detail="No Prometheus endpoint configured for this cluster.")

    cfg = mp.resolve(cluster.metrics_config)
    async with httpx.AsyncClient(timeout=6.0) as client:
        errors = await _query_scalar(client, base, mp.q_error_rate(cfg))
        latency = await _query_scalar(client, base, mp.q_latency_p95(cfg))
        cpu = await _query_scalar(client, base, mp.q_cpu(cfg))
        mem = await _query_scalar(client, base, mp.q_mem(cfg))

    def r(v: Optional[float], dp: int = 2) -> Optional[float]:
        return None if v is None or v != v else round(v, dp)

    return {"errors": r(errors), "latency": r(latency, 0), "cpu": r(cpu), "mem": r(mem)}
