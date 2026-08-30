"""Canonical incident investigation runner.

All production callers (Alertmanager webhook, incidents API, mission-control,
background workers) must invoke ``run_incident_investigation``. Historical
alternate runners are quarantined shims that forward here so behavior cannot
drift across duplicate implementations.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Stable import path for call-graph / documentation checks.
CANONICAL_ENTRYPOINT = "sre_agent.incident_runner.run_incident_investigation"


async def publish_incident_lifecycle(
    event_type: str,
    *,
    incident_id: uuid.UUID | str,
    alert_name: str,
    summary: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort lifecycle publish for live UI / Slack war-room consumers."""
    try:
        from .live_events import LiveEvent, get_event_bus
        from .war_room import INCIDENTS_CHANNEL

        payload = {
            "incident_id": str(incident_id),
            "alert_name": alert_name,
            "summary": summary or f"Investigating alert: {alert_name}",
        }
        if extra:
            payload.update(extra)
        await get_event_bus().publish(
            INCIDENTS_CHANNEL,
            LiveEvent(event_type, payload, str(incident_id)).to_dict(),
        )
    except Exception as err:
        logger.debug("incident-%s publish skipped: %s", event_type, err)


async def run_incident_investigation(
    incident_id: uuid.UUID,
    cluster_id: uuid.UUID,
    alert_name: str,
    job_id: Optional[uuid.UUID] = None,
    alert_labels: Optional[Dict[str, str]] = None,
    alert_annotations: Optional[Dict[str, str]] = None,
    alert_starts_at: Optional[str] = None,
    alert_severity: str = "warning",
    organization_id: Optional[str] = None,
    admission_owner: Optional[str] = None,
):
    """Single production entry point for SaaS incident graph invocation."""
    from sre_agent.agent_runtime import run_graph_background_saas

    return await run_graph_background_saas(
        incident_id=incident_id,
        cluster_id=cluster_id,
        alert_name=alert_name,
        job_id=job_id,
        alert_labels=alert_labels,
        alert_annotations=alert_annotations,
        alert_starts_at=alert_starts_at,
        alert_severity=alert_severity,
        organization_id=organization_id,
        admission_owner=admission_owner,
    )
