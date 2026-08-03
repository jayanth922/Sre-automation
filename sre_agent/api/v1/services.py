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


async def fetch_service_health(cluster: models.Cluster) -> List[Dict[str, Any]]:
    """Per-service golden signals (RED) for a cluster, from its Prometheus + profile.
    Returns [] when no Prometheus endpoint is configured. Shared by the API
    endpoint and the continuous platform monitor."""
    base = _prom_base(cluster)
    if not base:
        return []

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


@router.get("/{cluster_id}/services")
async def get_cluster_services(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> List[Dict[str, Any]]:
    """Per-service golden signals grouped by the cluster's configured service label."""
    cluster = await _load_cluster(cluster_id, user, db)
    if not _prom_base(cluster):
        raise HTTPException(status_code=503, detail="No Prometheus endpoint configured for this cluster.")
    return await fetch_service_health(cluster)


@router.get("/{cluster_id}/connections")
async def get_cluster_connections(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Preflight for a cluster's integrations — actively checks whether Prometheus,
    Loki, GitHub, and Notion are reachable/authorized, and whether Alertmanager has
    delivered anything. Lets a new user see at a glance if their setup is wired,
    instead of discovering it's broken only when an incident fails to open."""
    import os
    import asyncio
    from datetime import datetime, timezone

    cluster = await _load_cluster(cluster_id, user, db)
    prom = _prom_base(cluster)
    loki = (cluster.loki_url or os.getenv("LOKI_URL") or "").rstrip("/")
    repo = cluster.github_repo
    gh_token = os.getenv("GITHUB_TOKEN")
    notion_db = cluster.notion_database_id
    notion_key = cluster.notion_api_key

    def _row(name, configured, ok, detail):
        return {"name": name, "configured": configured, "ok": ok, "detail": detail}

    async def check_prometheus(client):
        if not prom:
            return _row("Prometheus", False, None, "Not set — add the URL in Settings (metrics won't be available).")
        try:
            r = await client.get(f"{prom}/api/v1/query", params={"query": "vector(1)"})
            if r.status_code == 200 and r.json().get("status") == "success":
                return _row("Prometheus", True, True, "Reachable and answering queries.")
            return _row("Prometheus", True, False, f"Responded {r.status_code}.")
        except Exception as e:
            return _row("Prometheus", True, False, f"Unreachable: {type(e).__name__}.")

    async def check_loki(client):
        if not loki:
            return _row("Loki (logs)", False, None, "Not set — the agent will investigate without logs.")
        try:
            r = await client.get(f"{loki}/ready")
            if r.status_code == 200:
                return _row("Loki (logs)", True, True, "Ready.")
            return _row("Loki (logs)", True, False, f"Responded {r.status_code}.")
        except Exception as e:
            return _row("Loki (logs)", True, False, f"Unreachable: {type(e).__name__}.")

    async def check_github(client):
        if not repo:
            return _row("GitHub", False, None, "No repo set — code context and revert PRs are off.")
        headers = {"Authorization": f"Bearer {gh_token}"} if gh_token else {}
        try:
            r = await client.get(f"https://api.github.com/repos/{repo}", headers=headers)
            if r.status_code == 200:
                return _row("GitHub", True, True, "Repo accessible.")
            if r.status_code in (401, 403):
                return _row("GitHub", True, False, "Token missing or invalid.")
            if r.status_code == 404:
                return _row("GitHub", True, False, "Repo not found — check the name or the token's scope.")
            return _row("GitHub", True, False, f"Responded {r.status_code}.")
        except Exception as e:
            return _row("GitHub", True, False, f"Unreachable: {type(e).__name__}.")

    async def check_notion(client):
        if not (notion_db and notion_key):
            return _row("Notion runbooks", False, None, "Not set — using the local runbook corpus.")
        try:
            r = await client.get(
                f"https://api.notion.com/v1/databases/{notion_db}",
                headers={"Authorization": f"Bearer {notion_key}", "Notion-Version": "2022-06-28"},
            )
            if r.status_code == 200:
                return _row("Notion runbooks", True, True, "Database accessible.")
            if r.status_code in (401, 403):
                return _row("Notion runbooks", True, False, "Token invalid, or the database isn't shared with the integration.")
            if r.status_code == 404:
                return _row("Notion runbooks", True, False, "Database not found — check the ID.")
            return _row("Notion runbooks", True, False, f"Responded {r.status_code}.")
        except Exception as e:
            return _row("Notion runbooks", True, False, f"Unreachable: {type(e).__name__}.")

    def check_alerts():
        hb = cluster.last_heartbeat
        if not hb:
            return _row("Alerts (Alertmanager)", True, None,
                        "No alerts received yet — point Alertmanager at the webhook with this cluster's token.")
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - hb).total_seconds()))
        human = f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h"
        return _row("Alerts (Alertmanager)", True, True, f"Last alert received {human} ago.")

    async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
        prom_r, loki_r, gh_r, notion_r = await asyncio.gather(
            check_prometheus(client), check_loki(client), check_github(client), check_notion(client)
        )

    return {
        "checks": [prom_r, loki_r, check_alerts(), gh_r, notion_r],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


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


@router.post("/{cluster_id}/query")
async def cluster_nl_query(
    cluster_id: uuid.UUID,
    payload: Dict[str, Any],
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Natural-language metric query. Translates the question to PromQL, verifies
    it (allow-listed metrics/functions, bounded window — never arbitrary code),
    then runs the *validated* query against this cluster's Prometheus."""
    from sre_agent.nl_query import plan_and_generate

    cluster = await _load_cluster(cluster_id, user, db)
    question = str((payload or {}).get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    plan = plan_and_generate(question)
    result: Dict[str, Any] = {
        "question": question,
        "promql": plan.promql,
        "valid": plan.valid,
        "executed": False,
        "data": None,
        "error": None if plan.valid else plan.reason,
    }
    if not plan.valid:
        return result

    base = _prom_base(cluster)
    if not base:
        result["error"] = "No Prometheus endpoint configured for this cluster."
        return result

    async with httpx.AsyncClient(timeout=6.0) as client:
        try:
            resp = await client.get(f"{base}/api/v1/query", params={"query": plan.promql})
            data = resp.json()
            if data.get("status") == "success":
                result["executed"] = True
                result["data"] = data["data"]["result"]
            else:
                result["error"] = "Prometheus rejected the query."
        except Exception as e:
            result["error"] = f"Execution failed: {e}"
    return result
