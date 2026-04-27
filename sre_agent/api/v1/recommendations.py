"""Per-cluster AI recommendations based on incident history."""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(prefix="/clusters", tags=["recommendations"])


async def _generate_recommendations(summary: Dict[str, Any]) -> str:
    from sre_agent.llm_utils import create_llm_with_fallback
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = create_llm_with_fallback()

    system = (
        "You are a senior SRE advisor. Given a cluster's 30-day incident data, "
        "provide exactly 4 specific, actionable recommendations to improve reliability. "
        "Format each as: **[Category]** Recommendation text. "
        "Categories: Alerting, Capacity, Deployment, Observability, Resilience, On-Call. "
        "Be direct. No intros or conclusions."
    )

    user_msg = (
        f"Cluster: {summary['cluster_name']}\n"
        f"30-day incidents: {summary['total']} total, {summary['resolved']} resolved "
        f"({summary['resolution_rate']}% rate)\n"
        f"MTTR: {summary['mttr_minutes']} minutes\n"
        f"Top alerts:\n" +
        "\n".join(f"  - {a['title']}: {a['count']}x" for a in summary["top_alerts"]) +
        f"\nSeverity: " +
        ", ".join(f"{s['severity']}: {s['count']}" for s in summary["severity"])
    )

    response = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_msg)])
    return response.content


@router.get("/{cluster_id}/recommendations")
async def get_cluster_recommendations(
    cluster_id: uuid.UUID,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
) -> Dict[str, Any]:
    """AI-generated reliability recommendations based on 30-day incident history."""
    cluster = await db.get(models.Cluster, cluster_id)
    if not cluster or cluster.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Cluster not found")

    since = datetime.now(timezone.utc) - timedelta(days=30)

    stats_row = (await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(models.Incident.status == models.IncidentStatus.RESOLVED).label("resolved"),
            func.avg(
                func.extract("epoch", models.Incident.resolved_at - models.Incident.created_at)
            ).filter(models.Incident.resolved_at.isnot(None)).label("avg_secs"),
        ).where(
            models.Incident.cluster_id == cluster_id,
            models.Incident.created_at >= since,
        )
    )).first()

    total = stats_row.total or 0
    resolved = stats_row.resolved or 0
    mttr = round((stats_row.avg_secs or 0) / 60, 1)
    rate = round(resolved / total * 100, 1) if total > 0 else 0.0

    if total == 0:
        return {
            "cluster_name": cluster.name,
            "recommendations": "No incidents in the last 30 days. System appears healthy — maintain current practices.",
            "stats": {"total": 0, "mttr_minutes": 0, "resolution_rate": 0.0},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    top_rows = (await db.execute(
        select(models.Incident.title, func.count().label("count"))
        .where(models.Incident.cluster_id == cluster_id, models.Incident.created_at >= since)
        .group_by(models.Incident.title)
        .order_by(func.count().desc())
        .limit(5)
    )).fetchall()

    sev_rows = (await db.execute(
        select(models.Incident.severity, func.count().label("count"))
        .where(models.Incident.cluster_id == cluster_id, models.Incident.created_at >= since)
        .group_by(models.Incident.severity)
        .order_by(func.count().desc())
    )).fetchall()

    summary = {
        "cluster_name": cluster.name,
        "total": total,
        "resolved": resolved,
        "resolution_rate": rate,
        "mttr_minutes": mttr,
        "top_alerts": [{"title": r.title, "count": r.count} for r in top_rows],
        "severity": [{"severity": r.severity.capitalize(), "count": r.count} for r in sev_rows],
    }

    recommendations = await _generate_recommendations(summary)

    return {
        "cluster_name": cluster.name,
        "recommendations": recommendations,
        "stats": {"total": total, "mttr_minutes": mttr, "resolution_rate": rate},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
