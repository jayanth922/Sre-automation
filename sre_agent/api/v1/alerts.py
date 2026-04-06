"""
Alert Webhook Router — Receives Alertmanager webhooks and creates incidents.

Flow: Alertmanager fires alert → POST /api/v1/alerts/webhook → create incident
      → trigger background SRE Agent investigation.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models, schemas

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/alerts",
    tags=["alerts"],
)


# ---------------------------------------------------------------------------
# Auth: reuse cluster-token authentication from agent_connect
# ---------------------------------------------------------------------------

async def _get_cluster_from_token(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(database.get_db),
) -> models.Cluster:
    """Authenticate via cluster token sent by Alertmanager's http_config."""
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("Webhook rejected: Missing or invalid Authorization header")
        raise HTTPException(status_code=403, detail="Missing or invalid cluster token")

    token = authorization.split(" ", 1)[1]
    cluster = await crud.get_cluster_by_token(db, token)
    if not cluster:
        logger.warning(f"Webhook rejected: Invalid cluster token provided (ends in ...{token[-4:]})")
        raise HTTPException(status_code=403, detail="Invalid cluster token")
    return cluster


# ---------------------------------------------------------------------------
# Helpers: parse Alertmanager payload
# ---------------------------------------------------------------------------

# Map Alertmanager severity labels → our IncidentSeverity enum
_SEVERITY_MAP = {
    "critical": models.IncidentSeverity.CRITICAL,
    "high":     models.IncidentSeverity.HIGH,
    "warning":  models.IncidentSeverity.MEDIUM,
    "info":     models.IncidentSeverity.LOW,
}


def _parse_alertmanager_payload(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract a flat list of alert dicts from the Alertmanager webhook body."""
    parsed: List[Dict[str, Any]] = []
    for alert in body.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        parsed.append({
            "status":      alert.get("status", "firing"),
            "alertname":   labels.get("alertname", "UnknownAlert"),
            "severity":    labels.get("severity", "warning"),
            "service":     labels.get("service", "unknown"),
            "summary":     annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "starts_at":   alert.get("startsAt", ""),
            "labels":      labels,
        })
    return parsed


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def receive_alertmanager_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    cluster: models.Cluster = Depends(_get_cluster_from_token),
    db: AsyncSession = Depends(database.get_db),
):
    """
    Receive an Alertmanager webhook, create incidents, and trigger investigations.

    Expected payload (standard Alertmanager v4 format):
    {
      "status": "firing",
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "...", "severity": "critical", "service": "..."},
          "annotations": {"summary": "...", "description": "..."},
          "startsAt": "2026-..."
        }
      ]
    }
    """
    body = await request.json()
    alerts = _parse_alertmanager_payload(body)

    if not alerts:
        return {"received": 0, "incidents_created": 0, "detail": "No alerts in payload"}

    incidents_created = 0

    for alert in alerts:
        # Skip resolved alerts — we only create incidents for firing ones
        if alert["status"] != "firing":
            logger.info(f"Skipping resolved alert: {alert['alertname']}")
            continue

        title = f"[{alert['service']}] {alert['alertname']}"
        description = (
            f"{alert['summary']}\n\n{alert['description']}\n\n"
            f"Labels: {json.dumps(alert['labels'], indent=2)}"
        )
        severity = _SEVERITY_MAP.get(alert["severity"], models.IncidentSeverity.MEDIUM)

        # Deduplicate: skip if an open incident with same title exists
        existing = await crud.find_duplicate_incident(db, cluster.id, title)
        if existing:
            logger.info(f"Dedup: '{title}' already open as incident {existing.id}")
            continue

        # Create incident
        incident_data = schemas.IncidentCreate(
            title=title,
            description=description,
            severity=severity,
        )
        incident = await crud.create_incident(db, incident_data, cluster.id)
        incidents_created += 1
        logger.info(f"Created incident {incident.id} for alert '{alert['alertname']}' on cluster {cluster.id}")

        # Create a Job for the SRE Edge Agent to pick up
        job_data = schemas.JobCreate(
            job_type=models.JobType.INVESTIGATION,
            payload=json.dumps({
                "alert": title,
                "incident_id": str(incident.id),
                "triggered_by": "alertmanager_webhook"
            })
        )
        job = await crud.create_job(db, cluster.id, job_data)
        logger.info(f"Queued job {job.id} for incident {incident.id}")

        # 🚀 START: SaaS-side background investigation
        from sre_agent.agent_runtime import run_graph_background_saas
        background_tasks.add_task(
            run_graph_background_saas,
            incident_id=incident.id,
            cluster_id=cluster.id,
            alert_name=alert["alertname"],
            job_id=job.id
        )
        logger.info(f"Launched background investigation for incident {incident.id}")

    return {
        "received": len(alerts),
        "incidents_created": incidents_created,
    }
