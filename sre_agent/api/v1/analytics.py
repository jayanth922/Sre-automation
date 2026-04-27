"""Analytics API — incident trends, severity distribution, MTTR per cluster."""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(prefix="/clusters", tags=["analytics"])


@router.get("/{cluster_id}/analytics")
async def get_cluster_analytics(
    cluster_id: uuid.UUID,
    weeks: int = 12,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """Incident trend data for a cluster: weekly counts, severity split, MTTR, top alerts."""
    cluster = await db.get(models.Cluster, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    since = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    # Weekly incident counts
    weekly_rows = await db.execute(
        select(
            func.date_trunc("week", models.Incident.created_at).label("week"),
            func.count().label("count"),
        )
        .where(
            models.Incident.cluster_id == cluster_id,
            models.Incident.created_at >= since,
        )
        .group_by("week")
        .order_by("week")
    )
    weekly = [
        {"week": row.week.strftime("%b %d"), "count": row.count}
        for row in weekly_rows
    ]

    # Severity distribution (all time)
    severity_rows = await db.execute(
        select(models.Incident.severity, func.count().label("count"))
        .where(models.Incident.cluster_id == cluster_id)
        .group_by(models.Incident.severity)
        .order_by(func.count().desc())
    )
    severity = [
        {"severity": row.severity.capitalize(), "count": row.count}
        for row in severity_rows
    ]

    # Summary stats (all time)
    stats_row = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(models.Incident.status == models.IncidentStatus.RESOLVED)
                .label("resolved"),
                func.avg(
                    func.extract(
                        "epoch",
                        models.Incident.resolved_at - models.Incident.created_at,
                    )
                )
                .filter(models.Incident.resolved_at.isnot(None))
                .label("avg_resolve_seconds"),
            ).where(models.Incident.cluster_id == cluster_id)
        )
    ).first()

    total = stats_row.total or 0
    resolved = stats_row.resolved or 0
    avg_secs = stats_row.avg_resolve_seconds or 0
    mttr_minutes = round(avg_secs / 60, 1)
    resolution_rate = round(resolved / total * 100, 1) if total > 0 else 0.0

    # Top 5 recurring alert titles
    top_rows = await db.execute(
        select(models.Incident.title, func.count().label("count"))
        .where(models.Incident.cluster_id == cluster_id)
        .group_by(models.Incident.title)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_alerts = [{"title": row.title, "count": row.count} for row in top_rows]

    return {
        "weekly_incidents": weekly,
        "severity_distribution": severity,
        "stats": {
            "total_incidents": total,
            "resolved": resolved,
            "resolution_rate_pct": resolution_rate,
            "mttr_minutes": mttr_minutes,
        },
        "top_alerts": top_alerts,
        "cluster_name": cluster.name,
    }
