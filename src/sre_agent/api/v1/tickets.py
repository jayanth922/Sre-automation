"""Jira ticket endpoints — manual create/link/status for an incident's ticket.

Complements the automatic open/resolve hooks in agent_runtime.py: lets the
console create a ticket for an incident that predates Jira configuration, or
re-check status on demand.
"""
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.integrations.jira import jira_configured, maybe_create_jira_issue

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/clusters",
    tags=["tickets"],
    dependencies=[Depends(get_current_user_and_org)],
)


class CreateTicketRequest(BaseModel):
    severity: Optional[str] = None


@router.get("/{cluster_id}/incidents/{incident_id}/ticket")
async def get_ticket(
    cluster_id: uuid.UUID,
    incident_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Report whether this incident has a linked Jira issue, and whether the
    cluster even has Jira configured."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    incident = await crud.get_incident_by_id(db, incident_id)
    if not incident or incident.cluster_id != cluster_id:
        raise HTTPException(status_code=404, detail="Incident not found")

    return {
        "jira_configured": jira_configured(cluster),
        "jira_issue_key": incident.jira_issue_key,
        "jira_issue_url": (
            f"{cluster.jira_url.rstrip('/')}/browse/{incident.jira_issue_key}"
            if cluster.jira_url and incident.jira_issue_key
            else None
        ),
    }


@router.post("/{cluster_id}/incidents/{incident_id}/ticket")
async def create_ticket(
    cluster_id: uuid.UUID,
    incident_id: uuid.UUID,
    body: CreateTicketRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Manually create a Jira issue for an incident that doesn't have one yet."""
    cluster = await crud.get_cluster_by_id(db, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    incident = await crud.get_incident_by_id(db, incident_id)
    if not incident or incident.cluster_id != cluster_id:
        raise HTTPException(status_code=404, detail="Incident not found")
    if not jira_configured(cluster):
        raise HTTPException(status_code=400, detail="Jira is not configured for this cluster")
    if incident.jira_issue_key:
        raise HTTPException(status_code=409, detail=f"Already linked to {incident.jira_issue_key}")

    await maybe_create_jira_issue(
        incident_id=str(incident.id),
        cluster_id=str(cluster.id),
        alert_name=incident.title,
        summary=incident.summary or incident.description or incident.title,
        severity=body.severity or getattr(incident.severity, "value", incident.severity),
    )
    await db.refresh(incident)
    if not incident.jira_issue_key:
        raise HTTPException(status_code=502, detail="Jira issue creation failed; check server logs")
    return {"jira_issue_key": incident.jira_issue_key}
