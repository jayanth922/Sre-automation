"""Truthful cluster connectivity / heartbeat evaluation.

Observed evidence (Alertmanager webhooks, edge token auth, explicit probes)
updates ``last_heartbeat``. A reconcile loop only reclassifies status from the
stored timestamp — it never fabricates freshness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.models import ClusterStatus

HEARTBEAT_SOURCE_ALERTMANAGER = "alertmanager"
HEARTBEAT_SOURCE_EDGE = "edge"
HEARTBEAT_SOURCE_PROBE = "probe"


def online_threshold_seconds() -> float:
    return float(os.getenv("CLUSTER_HEARTBEAT_ONLINE_SECONDS", "120"))


def degraded_threshold_seconds() -> float:
    return float(os.getenv("CLUSTER_HEARTBEAT_DEGRADED_SECONDS", "300"))


def stale_threshold_seconds() -> float:
    return float(os.getenv("CLUSTER_HEARTBEAT_STALE_SECONDS", "900"))


@dataclass(frozen=True)
class HeartbeatEvaluation:
    status: ClusterStatus
    reason: str
    age_seconds: Optional[float]
    source: Optional[str]


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def evaluate_heartbeat(
    last_heartbeat: Optional[datetime],
    *,
    source: Optional[str] = None,
    now: Optional[datetime] = None,
    maintenance: bool = False,
) -> HeartbeatEvaluation:
    """Classify connectivity from observed heartbeat evidence only."""
    if maintenance:
        return HeartbeatEvaluation(
            status=ClusterStatus.MAINTENANCE,
            reason="maintenance",
            age_seconds=None,
            source=source,
        )

    observed = _aware(last_heartbeat)
    if observed is None:
        return HeartbeatEvaluation(
            status=ClusterStatus.OFFLINE,
            reason="never_seen",
            age_seconds=None,
            source=source,
        )

    current = _aware(now) or datetime.now(timezone.utc)
    age = max(0.0, (current - observed).total_seconds())
    src = source or "unknown"

    if age <= online_threshold_seconds():
        return HeartbeatEvaluation(
            status=ClusterStatus.ONLINE,
            reason=f"fresh:{src}",
            age_seconds=age,
            source=source,
        )
    if age <= degraded_threshold_seconds():
        return HeartbeatEvaluation(
            status=ClusterStatus.DEGRADED,
            reason=f"degraded:{src}",
            age_seconds=age,
            source=source,
        )
    if age <= stale_threshold_seconds():
        return HeartbeatEvaluation(
            status=ClusterStatus.STALE,
            reason=f"stale:{src}",
            age_seconds=age,
            source=source,
        )
    return HeartbeatEvaluation(
        status=ClusterStatus.OFFLINE,
        reason=f"offline:{src}",
        age_seconds=age,
        source=source,
    )


def heartbeat_payload(
    last_heartbeat: Optional[datetime],
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """API/dashboard payload with status, source, reason, and age."""
    status_value = getattr(status, "value", status)
    evaluation = evaluate_heartbeat(
        last_heartbeat,
        source=source,
        now=now,
        maintenance=(status_value == ClusterStatus.MAINTENANCE.value),
    )
    observed = _aware(last_heartbeat)
    effective_status = (
        ClusterStatus.MAINTENANCE.value
        if status_value == ClusterStatus.MAINTENANCE.value
        else evaluation.status.value
    )
    return {
        "status": effective_status,
        "last_heartbeat": observed.isoformat() if observed else None,
        "heartbeat_source": evaluation.source or source,
        "heartbeat_reason": reason or evaluation.reason,
        "age_seconds": evaluation.age_seconds,
    }
