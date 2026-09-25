"""Organization-scoped resource dependencies for API v1 routes."""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org


def _not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} not found",
    )


async def get_owned_cluster(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> models.Cluster:
    """Load a cluster only when it belongs to the caller's organization."""
    result = await db.execute(
        select(models.Cluster).where(
            models.Cluster.id == cluster_id,
            models.Cluster.org_id == user.org_id,
        )
    )
    cluster = result.scalars().first()
    if cluster is None:
        raise _not_found("Cluster")
    return cluster


async def get_owned_incident(
    incident_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> models.Incident:
    """Load an incident through an organization-owned cluster."""
    result = await db.execute(
        select(models.Incident)
        .join(models.Cluster, models.Incident.cluster_id == models.Cluster.id)
        .where(
            models.Incident.id == incident_id,
            models.Cluster.org_id == user.org_id,
        )
    )
    incident = result.scalars().first()
    if incident is None:
        raise _not_found("Incident")
    return incident


async def get_owned_slo(
    slo_id: uuid.UUID,
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> models.SLO:
    """Load an SLO only under the requested organization-owned cluster."""
    result = await db.execute(
        select(models.SLO)
        .join(models.Cluster, models.SLO.cluster_id == models.Cluster.id)
        .where(
            models.SLO.id == slo_id,
            models.SLO.cluster_id == cluster_id,
            models.Cluster.org_id == user.org_id,
        )
    )
    slo = result.scalars().first()
    if slo is None:
        raise _not_found("SLO")
    return slo
