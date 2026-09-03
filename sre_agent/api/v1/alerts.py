"""
Alert Webhook Router — Receives Alertmanager webhooks and creates incidents.

Flow: Alertmanager fires alert → POST /api/v1/alerts/webhook → create incident
      → trigger background SRE Agent investigation.

Resolved notifications correlate to the active incident and apply verification
rules (never masking ``REMEDIATION_FAILED``).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models, schemas
from sre_agent.alert_resolution import reconcile_resolved_alert
from sre_agent.incident_correlation import CorrelationCandidate, correlate

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
        logger.warning("Webhook rejected: Invalid cluster token provided")
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
            "ends_at":     alert.get("endsAt", ""),
            "labels":      labels,
        })
    return parsed


def _incident_title(alert: Dict[str, Any]) -> str:
    return f"[{alert['service']}] {alert['alertname']}"


def _status_str(status: Any) -> Optional[str]:
    if status is None:
        return None
    return str(getattr(status, "value", status))


async def _record_correlation_shadow(
    db: AsyncSession,
    cluster: models.Cluster,
    incident: models.Incident,
) -> None:
    """Phase 5 correlation gate, shadow mode (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md,
    Phase A): score the just-created incident against every other open incident
    in its cluster and record what it *would* bundle with, purely as an
    observational timeline event. Never changes dedup, investigation, or
    execution behavior — this is validation data before the correlation gate
    is allowed to act on anything.
    """
    open_incidents = await crud.list_active_incidents_for_cluster(
        db, cluster.id, exclude_incident_id=incident.id
    )
    if not open_incidents:
        return

    candidate = CorrelationCandidate(
        incident_id=str(incident.id),
        cluster_id=str(cluster.id),
        title=incident.title,
        description=incident.description or "",
        created_at=incident.created_at,
    )
    pool = [
        CorrelationCandidate(
            incident_id=str(other.id),
            cluster_id=str(cluster.id),
            title=other.title,
            description=other.description or "",
            created_at=other.created_at,
        )
        for other in open_incidents
    ]
    adjacency = None
    try:
        from sre_agent.service_topology import get_adjacency_map

        adjacency = await get_adjacency_map(cluster)
    except Exception as e:
        logger.warning(f"Correlation shadow: adjacency fetch failed for cluster {cluster.id}: {e}")

    result = correlate(candidate, pool, adjacency=adjacency)
    if result.decision != "bundle":
        return

    await crud.create_incident_timeline_event(
        db,
        incident.id,
        event_type="correlation_shadow",
        speaker_role="system",
        title="Correlation gate (shadow mode)",
        content=(
            f"Would bundle with incident {result.bundle_with} "
            f"(score={result.best_score:.2f}): {'; '.join(result.matches[0].reasons)}"
        ),
        payload={
            "source": "incident_correlation",
            "mode": "shadow",
            "bundle_with": result.bundle_with,
            "score": result.best_score,
            "matches": [
                {"incident_id": m.incident_id, "score": m.score, "reasons": m.reasons}
                for m in result.matches
            ],
        },
    )
    logger.info(
        "Correlation shadow: incident %s would bundle with %s (score=%.2f)",
        incident.id,
        result.bundle_with,
        result.best_score,
    )


async def _reconcile_resolved_alert(
    db: AsyncSession,
    cluster: models.Cluster,
    alert: Dict[str, Any],
) -> Dict[str, Any]:
    """Correlate a resolved alert to an active incident and apply status rules."""
    title = _incident_title(alert)
    incident = await crud.find_active_incident_by_title(db, cluster.id, title)
    if not incident:
        logger.info("Resolved alert '%s' had no active incident to reconcile", alert["alertname"])
        return {
            "alertname": alert["alertname"],
            "matched": False,
            "reason": "no_active_incident",
        }

    decision = reconcile_resolved_alert(incident.status)
    values: Dict[str, Any] = {}
    if decision.mark_resolved and decision.new_status is not None:
        values["status"] = models.IncidentStatus(decision.new_status)
        values["resolved_at"] = datetime.now(timezone.utc)
    elif (
        decision.new_status is not None
        and decision.new_status != _status_str(incident.status)
    ):
        values["status"] = models.IncidentStatus(decision.new_status)

    if values:
        await db.execute(
            update(models.Incident)
            .where(models.Incident.id == incident.id)
            .values(**values)
        )
        await db.commit()

    await crud.create_incident_timeline_event(
        db,
        incident.id,
        event_type="alert_resolved",
        speaker_role="system",
        title="Alertmanager resolved notification",
        content=(
            f"Alert `{alert['alertname']}` cleared externally. "
            f"{decision.reason}."
            + (
                " Remediation failure preserved; incident not marked resolved."
                if decision.masked_failed_remediation
                else ""
            )
        ),
        payload={
            "alertname": alert["alertname"],
            "previous_status": str(decision.previous_status) if decision.previous_status else None,
            "new_status": str(decision.new_status) if decision.new_status else None,
            "mark_resolved": decision.mark_resolved,
            "masked_failed_remediation": decision.masked_failed_remediation,
            "ends_at": alert.get("ends_at") or None,
            "labels": alert.get("labels") or {},
        },
    )
    logger.info(
        "Reconciled resolved alert '%s' → incident %s (%s → %s, mark_resolved=%s)",
        alert["alertname"],
        incident.id,
        decision.previous_status,
        decision.new_status,
        decision.mark_resolved,
    )
    return {
        "alertname": alert["alertname"],
        "incident_id": str(incident.id),
        "matched": True,
        "previous_status": str(decision.previous_status) if decision.previous_status else None,
        "new_status": str(decision.new_status) if decision.new_status else None,
        "mark_resolved": decision.mark_resolved,
        "masked_failed_remediation": decision.masked_failed_remediation,
        "reason": decision.reason,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def receive_alertmanager_webhook(
    request: Request,
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

    # Authenticating with the cluster token and delivering a webhook is observed
    # Alertmanager connectivity evidence, even when every alert is deduped.
    await crud.update_cluster_heartbeat(
        db,
        cluster.id,
        source="alertmanager",
        reason="alertmanager_webhook",
    )

    if not alerts:
        return {
            "received": 0,
            "incidents_created": 0,
            "resolved_reconciled": 0,
            "detail": "No alerts in payload",
        }

    incidents_created = 0
    resolved_reconciled = 0
    reconciliations: List[Dict[str, Any]] = []

    for alert in alerts:
        if alert["status"] != "firing":
            result = await _reconcile_resolved_alert(db, cluster, alert)
            reconciliations.append(result)
            if result.get("matched"):
                resolved_reconciled += 1
            continue

        title = _incident_title(alert)
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

        try:
            await _record_correlation_shadow(db, cluster, incident)
        except Exception as correlation_err:
            logger.warning(f"Correlation shadow check failed (non-fatal): {correlation_err}")

        # Create a durable investigation job (lease-backed; survives process loss).
        from sre_agent.job_worker import enqueue_and_kick

        job = await enqueue_and_kick(
            db=db,
            cluster_id=cluster.id,
            organization_id=cluster.org_id,
            incident_id=incident.id,
            alert_name=alert["alertname"],
            alert_labels=alert.get("labels") or {},
            alert_annotations={
                "summary": alert.get("summary", ""),
                "description": alert.get("description", ""),
            },
            alert_starts_at=alert.get("startsAt") or alert.get("starts_at"),
            alert_severity=alert.get("severity") or "warning",
            triggered_by="alertmanager_webhook",
        )
        logger.info(f"Queued durable job {job.id} for incident {incident.id}")

    return {
        "received": len(alerts),
        "incidents_created": incidents_created,
        "resolved_reconciled": resolved_reconciled,
        "reconciliations": reconciliations,
    }
