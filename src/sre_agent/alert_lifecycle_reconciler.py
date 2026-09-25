"""Recover alert lifecycle clears that could not reach the control plane.

Alertmanager's resolved webhook is the normal close signal. If Alertmanager
and the API are both unavailable until an alert expires, that one-shot signal
can be lost. This module reconstructs only the missing *lifecycle* event from
Prometheus rule state. It never marks a remediation successful.

The evidence contract is deliberately conservative:

* the original Alertmanager identity must still be available in a durable job;
* the corresponding Prometheus alert rule must exist and report ``health=ok``;
* two snapshots must show no matching active series;
* the first snapshot is persisted on the incident timeline and the second must
  arrive after a grace interval, so an API restart cannot erase the evidence;
* transport errors, malformed responses, missing rules, and unhealthy rules
  are all ``unavailable`` rather than "clear".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

ABSENCE_EVENT = "alert_source_absence_observed"
FIRING_EVENT = "alert_source_firing_observed"
RESOLVED_EVENT = "alert_resolved"

_DEFAULT_CONFIRM_SECONDS = 300.0
_DEFAULT_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class AlertIdentity:
    alertname: str
    service: str
    labels: dict[str, Any]


@dataclass(frozen=True)
class AlertRuleSnapshot:
    available: bool
    rules: tuple[dict[str, Any], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class AlertSourceObservation:
    state: str  # firing | absent | unavailable
    reason: str


@dataclass(frozen=True)
class MissedResolutionRecovery:
    incident_id: str
    alertname: str
    previous_status: Optional[str]
    new_status: Optional[str]


def confirmation_delay() -> timedelta:
    try:
        seconds = float(
            os.getenv(
                "ALERT_RESOLUTION_RECONCILE_CONFIRM_SECONDS",
                str(_DEFAULT_CONFIRM_SECONDS),
            )
        )
    except ValueError:
        seconds = _DEFAULT_CONFIRM_SECONDS
    return timedelta(seconds=max(30.0, seconds))


def _probe_timeout() -> float:
    try:
        seconds = float(
            os.getenv(
                "ALERT_RESOLUTION_RECONCILE_TIMEOUT_SECONDS",
                str(_DEFAULT_TIMEOUT_SECONDS),
            )
        )
    except ValueError:
        seconds = _DEFAULT_TIMEOUT_SECONDS
    return min(30.0, max(1.0, seconds))


def alert_identity_from_job_payload(raw_payload: Any) -> Optional[AlertIdentity]:
    """Recover the exact source identity recorded before investigation began."""
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("triggered_by") != "alertmanager_webhook":
        return None
    alertname = str(payload.get("alert_name") or "").strip()
    labels = payload.get("alert_labels")
    if not alertname or not isinstance(labels, dict):
        return None
    return AlertIdentity(
        alertname=alertname,
        service=str(labels.get("service") or "unknown"),
        labels=dict(labels),
    )


async def fetch_alert_rule_snapshot(prometheus_url: str) -> AlertRuleSnapshot:
    """Read rule health and active alert instances in one Prometheus snapshot."""
    base = (prometheus_url or "").rstrip("/")
    if not base:
        return AlertRuleSnapshot(False, reason="prometheus_not_configured")

    try:
        async with httpx.AsyncClient(timeout=_probe_timeout()) as client:
            response = await client.get(
                f"{base}/api/v1/rules", params={"type": "alert"}
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return AlertRuleSnapshot(
            False, reason=f"prometheus_rule_query_failed:{type(exc).__name__}"
        )

    try:
        if payload.get("status") != "success":
            raise ValueError("non-success status")
        groups = payload["data"]["groups"]
        if not isinstance(groups, list):
            raise TypeError("groups is not a list")
        rules = tuple(
            rule
            for group in groups
            if isinstance(group, dict)
            for rule in group.get("rules", [])
            if isinstance(rule, dict) and rule.get("type") == "alerting"
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return AlertRuleSnapshot(
            False, reason=f"prometheus_rule_response_invalid:{type(exc).__name__}"
        )
    return AlertRuleSnapshot(True, rules=rules, reason="healthy_snapshot")


def observe_alert_source(
    snapshot: AlertRuleSnapshot, identity: AlertIdentity
) -> AlertSourceObservation:
    """Classify one alert from a rule snapshot without turning doubt into clear."""
    if not snapshot.available:
        return AlertSourceObservation("unavailable", snapshot.reason)

    matching = [
        rule
        for rule in snapshot.rules
        if str(rule.get("name") or "") == identity.alertname
    ]
    if not matching:
        # An empty ALERTS query cannot distinguish "inactive" from "the rule
        # disappeared / Prometheus restarted with the wrong config". Missing
        # rule metadata therefore provides no lifecycle evidence.
        return AlertSourceObservation("unavailable", "alert_rule_missing")
    if any(str(rule.get("health") or "").lower() != "ok" for rule in matching):
        return AlertSourceObservation("unavailable", "alert_rule_unhealthy")

    for rule in matching:
        alerts = rule.get("alerts", [])
        if alerts is None:
            alerts = []
        if not isinstance(alerts, list):
            return AlertSourceObservation("unavailable", "alert_instances_invalid")
        for active in alerts:
            if not isinstance(active, dict):
                return AlertSourceObservation(
                    "unavailable", "alert_instance_invalid"
                )
            labels = active.get("labels") or {}
            if not isinstance(labels, dict):
                return AlertSourceObservation(
                    "unavailable", "alert_instance_labels_invalid"
                )
            active_service = labels.get("service")
            if (
                identity.service == "unknown"
                or active_service is None
                or str(active_service) == identity.service
            ):
                # Pending is conservative evidence that the condition exists
                # again; Prometheus includes only active instances here.
                return AlertSourceObservation("firing", "matching_series_active")
    return AlertSourceObservation("absent", "healthy_rule_has_no_matching_series")


def _as_aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _load_candidates(db: Any) -> list[tuple[Any, Any]]:
    from sqlalchemy import select

    from backend import models

    statuses = (
        models.IncidentStatus.OPEN,
        models.IncidentStatus.INVESTIGATING,
        models.IncidentStatus.INVESTIGATED,
        models.IncidentStatus.AWAITING_APPROVAL,
        models.IncidentStatus.REMEDIATION_IN_PROGRESS,
        models.IncidentStatus.REMEDIATION_FAILED,
        models.IncidentStatus.VERIFICATION_UNKNOWN,
    )
    result = await db.execute(
        select(models.Incident, models.Cluster)
        .join(models.Cluster, models.Cluster.id == models.Incident.cluster_id)
        .where(
            models.Incident.status.in_(statuses),
            models.Cluster.prometheus_url.isnot(None),
            models.Cluster.prometheus_url != "",
        )
        .order_by(models.Incident.created_at.asc())
    )
    return list(result.all())


async def _load_alert_identity(db: Any, incident_id: Any) -> Optional[AlertIdentity]:
    from sqlalchemy import select

    from backend import models

    result = await db.execute(
        select(models.Job.payload)
        .where(
            models.Job.incident_id == incident_id,
            models.Job.job_type == models.JobType.INVESTIGATION,
            models.Job.payload.isnot(None),
        )
        .order_by(models.Job.created_at.desc())
    )
    for raw_payload in result.scalars().all():
        identity = alert_identity_from_job_payload(raw_payload)
        if identity is not None:
            return identity
    return None


async def _latest_source_event(db: Any, incident_id: Any) -> Optional[Any]:
    from sqlalchemy import select

    from backend import models

    result = await db.execute(
        select(models.IncidentTimelineEvent)
        .where(
            models.IncidentTimelineEvent.incident_id == incident_id,
            models.IncidentTimelineEvent.event_type.in_(
                (ABSENCE_EVENT, FIRING_EVENT, RESOLVED_EVENT)
            ),
        )
        .order_by(models.IncidentTimelineEvent.sequence.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _record_source_event(
    db: Any,
    incident: Any,
    identity: AlertIdentity,
    *,
    event_type: str,
    now: datetime,
) -> Any:
    from backend import crud

    if event_type == ABSENCE_EVENT:
        title = "Possible missed alert clear observed"
        content = (
            f"Prometheus rule `{identity.alertname}` is healthy and exposes no "
            f"active series matching `{incident.title}`. The incident remains "
            "open until a later healthy snapshot confirms the absence. This is "
            "alert-lifecycle evidence only; it does not prove a Sentinel "
            "remediation succeeded."
        )
    else:
        title = "Alert source is active"
        content = (
            f"Prometheus again reports an active series matching `{incident.title}`. "
            "Any pending missed-clear observation was discarded."
        )
    return await crud.create_incident_timeline_event(
        db,
        incident.id,
        event_type=event_type,
        speaker_role="system",
        title=title,
        content=content,
        payload={
            "alertname": identity.alertname,
            "service": identity.service,
            "observed_at": now.isoformat(),
            "evidence": "prometheus_alert_rule_snapshot",
            "remediation_verified": False,
        },
    )


async def reconcile_missed_alert_resolutions(
    *,
    now: Optional[datetime] = None,
    confirm_after: Optional[timedelta] = None,
    snapshot_loader: Callable[[str], Awaitable[AlertRuleSnapshot]] = fetch_alert_rule_snapshot,
) -> list[MissedResolutionRecovery]:
    """Recover missed resolved webhooks from two durable, healthy snapshots."""
    from backend import database
    from sre_agent.api.v1.alerts import _reconcile_resolved_alert

    now = _as_aware(now) or datetime.now(timezone.utc)
    confirm_after = confirm_after if confirm_after is not None else confirmation_delay()
    recovered: list[MissedResolutionRecovery] = []

    async with database.AsyncSessionLocal() as db:
        candidates = await _load_candidates(db)
        snapshots: dict[str, AlertRuleSnapshot] = {}
        for incident, cluster in candidates:
            identity = await _load_alert_identity(db, incident.id)
            if identity is None:
                continue
            latest = await _latest_source_event(db, incident.id)
            if latest is not None and latest.event_type == RESOLVED_EVENT:
                continue

            key = str(cluster.id)
            if key not in snapshots:
                snapshots[key] = await snapshot_loader(cluster.prometheus_url)
            observation = observe_alert_source(snapshots[key], identity)
            if observation.state == "unavailable":
                logger.info(
                    "missed-clear reconciliation unavailable for incident %s: %s",
                    incident.id,
                    observation.reason,
                )
                continue
            if observation.state == "firing":
                if latest is not None and latest.event_type == ABSENCE_EVENT:
                    await _record_source_event(
                        db,
                        incident,
                        identity,
                        event_type=FIRING_EVENT,
                        now=now,
                    )
                continue

            first_absence_at = _as_aware(
                getattr(latest, "created_at", None)
                if latest is not None and latest.event_type == ABSENCE_EVENT
                else None
            )
            if first_absence_at is None:
                await _record_source_event(
                    db,
                    incident,
                    identity,
                    event_type=ABSENCE_EVENT,
                    now=now,
                )
                continue
            if now - first_absence_at < confirm_after:
                continue

            result = await _reconcile_resolved_alert(
                db,
                cluster,
                {
                    "alertname": identity.alertname,
                    "service": identity.service,
                    "labels": identity.labels,
                    "ends_at": None,
                },
                reconciliation_source="prometheus_rule_recovery",
            )
            if result.get("matched"):
                recovered.append(
                    MissedResolutionRecovery(
                        incident_id=str(incident.id),
                        alertname=identity.alertname,
                        previous_status=result.get("previous_status"),
                        new_status=result.get("new_status"),
                    )
                )

    if recovered:
        logger.warning(
            "reconciler: recovered %d missed Alertmanager resolution(s): %s",
            len(recovered),
            ", ".join(item.incident_id for item in recovered),
        )
    return recovered
