"""Agent flight-recorder helpers: retention, export, and durable writes."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models import AgentAuditLog


def audit_retention_days() -> int:
    """How long to keep agent_audit_logs. ``0`` disables automatic purge."""
    raw = os.getenv("AGENT_AUDIT_RETENTION_DAYS", "90").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 90


def purge_expired_agent_audit_logs(
    session: Session, *, now: Optional[datetime] = None
) -> int:
    """Delete flight-recorder rows older than the configured retention window."""
    days = audit_retention_days()
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    result = session.execute(
        delete(AgentAuditLog).where(AgentAuditLog.timestamp < cutoff)
    )
    session.commit()
    return int(result.rowcount or 0)


def export_agent_audit_logs(
    session: Session,
    *,
    organization_id: Optional[uuid.UUID] = None,
    cluster_id: Optional[uuid.UUID] = None,
    incident_id: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Export queryable flight-recorder rows for compliance / mission-control."""
    stmt = select(AgentAuditLog).order_by(AgentAuditLog.timestamp.desc()).limit(limit)
    if organization_id is not None:
        stmt = stmt.where(AgentAuditLog.organization_id == organization_id)
    if cluster_id is not None:
        stmt = stmt.where(AgentAuditLog.cluster_id == cluster_id)
    if incident_id is not None:
        stmt = stmt.where(AgentAuditLog.incident_id == str(incident_id))
    if run_id is not None:
        stmt = stmt.where(AgentAuditLog.run_id == str(run_id))
    rows: Iterable[AgentAuditLog] = session.execute(stmt).scalars().all()
    return [
        {
            "id": str(row.id),
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "organization_id": (
                str(row.organization_id) if row.organization_id else None
            ),
            "cluster_id": str(row.cluster_id) if row.cluster_id else None,
            "incident_id": row.incident_id,
            "run_id": row.run_id,
            "agent_name": row.agent_name,
            "tool_name": row.tool_name,
            "tool_args": row.tool_args,
            "status": row.status,
            "result": row.result,
            "error_message": row.error_message,
        }
        for row in rows
    ]


def parse_optional_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
