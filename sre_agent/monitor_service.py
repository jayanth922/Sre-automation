"""Continuous platform monitor.

Runs as a background task inside the API. On each tick it walks every connected
cluster, evaluates per-service health from that cluster's own Prometheus (via
the shared service-health path — no demo assumptions), publishes a health
snapshot to the live insights bus, and opens an incident (deduped) for any
service that crosses the critical threshold — driving the same investigation
pipeline the alert webhook uses.

Generic by construction: it discovers clusters and services from real data;
nothing is hardcoded to any one workload.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

logger = logging.getLogger(__name__)


def _interval() -> int:
    try:
        return max(15, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


async def _tick() -> None:
    from backend import crud, database, models, schemas
    from sre_agent.api.v1.services import fetch_service_health
    from sre_agent.live_events import publish_insight

    async with database.AsyncSessionLocal() as db:
        clusters = (await db.execute(select(models.Cluster))).scalars().all()
        for cluster in clusters:
            try:
                services = await fetch_service_health(cluster)
            except Exception as e:
                logger.debug(f"monitor: health fetch failed for {cluster.name}: {e}")
                continue
            if not services:
                continue

            # Publish a health snapshot for the live insights view.
            try:
                await publish_insight(
                    {
                        "kind": "cluster_health",
                        "cluster_id": str(cluster.id),
                        "cluster": cluster.name,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "services": [
                            {
                                "name": s["name"],
                                "status": s["status"],
                                "error_pct": s["error_pct"],
                                "p95_ms": s["p95_ms"],
                                "rps": s["rps"],
                            }
                            for s in services
                        ],
                        "unhealthy": [s["name"] for s in services if s["status"] != "ok"],
                    }
                )
            except Exception as e:
                logger.debug(f"monitor: insight publish failed: {e}")

            # Open incidents for critical services (deduped by title while open).
            for s in services:
                if s["status"] != "crit":
                    continue
                title = f"{s['name']}: elevated error rate / latency"
                try:
                    existing = await crud.find_duplicate_incident(db, cluster.id, title)
                    if existing:
                        continue
                    desc = (
                        f"Detected by continuous monitoring. {s['name']} is unhealthy — "
                        f"error rate {s['error_pct']}%, p95 latency {s['p95_ms']}ms. "
                        f"Labels: {{\"service\": \"{s['name']}\"}}"
                    )
                    incident = await crud.create_incident(
                        db,
                        schemas.IncidentCreate(
                            title=title,
                            description=desc,
                            severity=models.IncidentSeverity.HIGH,
                        ),
                        cluster.id,
                    )
                    logger.info(f"monitor: opened incident {incident.id} for {s['name']} on {cluster.name}")
                    asyncio.create_task(_investigate(incident.id, cluster.id, title, s["name"]))
                except Exception as e:
                    logger.warning(f"monitor: failed to open incident for {s['name']}: {e}")


async def _investigate(incident_id, cluster_id, title: str, service: str) -> None:
    try:
        from sre_agent.agent_runtime import run_graph_background_saas

        await run_graph_background_saas(
            incident_id=incident_id,
            cluster_id=cluster_id,
            alert_name=title,
            alert_labels={"service": service},
            alert_annotations={"summary": f"Continuous monitor flagged {service}"},
            alert_severity="warning",
        )
    except Exception as e:
        logger.warning(f"monitor: investigation trigger failed for {incident_id}: {e}")


async def run_platform_monitor() -> None:
    """Background loop. Never raises out — a bad tick is logged and retried."""
    interval = _interval()
    logger.info(f"🩺 Platform monitor started (every {interval}s)")
    # Small delay so the app finishes coming up before the first sweep.
    await asyncio.sleep(min(interval, 20))
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.warning(f"monitor tick failed (non-fatal): {e}")
        await asyncio.sleep(interval)
