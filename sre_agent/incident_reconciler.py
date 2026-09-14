#!/usr/bin/env python3
"""Recover incidents whose remediation died with the process that was running it.

`approval_flow.decide_action_approval` commits the approval as APPROVED and
then drives the whole post-approval remediation *synchronously in the caller's
process* (`graph.astream`). Nothing enqueues a durable job for that resume, so
unlike an investigation it has no lease, no owner and no retry. If the process
dies between the cluster write and the verification write — a restart, a
deploy, an OOM kill — the incident is left in `REMEDIATION_IN_PROGRESS`, which
`incident_status.compute_incident_status` only ever returns as a *transient*
state ("a mutation executed, verification has not run yet"). The approval is
already consumed, so it cannot be re-approved; no job reclaims it; and the last
thing the on-call heard in Slack was "remediation is running".

Observed live on 2026-09-14: incident c9e6fc3d was approved at 03:35:35 and was
still `remediation_in_progress` three hours later with its alert continuously
firing and not one Slack message in between.

This module closes the honesty half of that gap. It does not re-run the
remediation: the graph checkpoint survives, but replaying it could re-apply a
cluster write that is not known to be idempotent, and guessing is worse than
saying so. Instead a stranded incident is moved to the status that is actually
true — `VERIFICATION_UNKNOWN`, "something was applied, nothing confirmed it" —
and the on-call is told, in the incident's own Slack thread, that the run was
interrupted and needs a human.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_STALE_MINUTES = 20.0
_DEFAULT_SWEEP_SECONDS = 300.0

_SWEEP_TASK: Optional[asyncio.Task] = None
_STOP = asyncio.Event()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stale_after() -> timedelta:
    """How long a remediation may sit silent before it is presumed dead.

    Must comfortably exceed a legitimate in-flight remediation: live
    verification polls the alert for minutes (the dc1712ca run confirmed at
    330s), so the default is deliberately several times that.
    """
    try:
        minutes = float(
            os.getenv("INCIDENT_STALE_REMEDIATION_MINUTES", str(_DEFAULT_STALE_MINUTES))
        )
    except ValueError:
        minutes = _DEFAULT_STALE_MINUTES
    return timedelta(minutes=max(1.0, minutes))


def sweep_interval() -> float:
    try:
        return max(
            30.0,
            float(os.getenv("INCIDENT_RECONCILE_SECONDS", str(_DEFAULT_SWEEP_SECONDS))),
        )
    except ValueError:
        return _DEFAULT_SWEEP_SECONDS


@dataclass(frozen=True)
class InterruptedRemediation:
    """One incident this sweep took out of a state nobody owned."""

    incident_id: str
    title: str
    last_activity: datetime
    silent_for: timedelta
    new_status: str
    slack_notified: bool


def _as_aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def last_activity_at(
    *,
    created_at: Any,
    timeline_at: Any = None,
    decided_at: Any = None,
    manifest_at: Any = None,
) -> datetime:
    """The most recent moment this incident demonstrably made progress.

    `incidents` has no `updated_at`, so "how long has this been silent?" has to
    be reconstructed from the rows that a live run keeps writing: timeline
    events, the approval decision that started the remediation, and the run
    manifest. Taking the max of all of them is what keeps a slow-but-alive run
    from being declared dead.
    """
    candidates = [
        _as_aware(created_at),
        _as_aware(timeline_at),
        _as_aware(decided_at),
        _as_aware(manifest_at),
    ]
    present = [c for c in candidates if c is not None]
    if not present:
        return datetime.min.replace(tzinfo=timezone.utc)
    return max(present)


def is_interrupted(
    *, last_activity: datetime, now: Optional[datetime] = None, threshold: Optional[timedelta] = None
) -> bool:
    """True when nothing has touched this remediation for longer than the threshold."""
    now = now or utc_now()
    threshold = threshold if threshold is not None else stale_after()
    return (now - last_activity) > threshold


def interrupted_message(*, title: str, silent_for: timedelta) -> str:
    """What the on-call reads in the thread. Says what is and is not known."""
    minutes = int(silent_for.total_seconds() // 60)
    return (
        ":warning: *Remediation interrupted*\n"
        f"The approved remediation for *{title}* stopped reporting "
        f"{minutes} minute(s) ago and the process running it is gone.\n"
        "A change may already have been applied to the cluster, but nothing "
        "verified it, so this incident is *not* known to be fixed.\n"
        "Status moved to `verification_unknown`. This needs a human: check the "
        "cluster state, then re-run or close it out."
    )


async def reconcile_interrupted_remediations(
    *, now: Optional[datetime] = None, threshold: Optional[timedelta] = None
) -> list[InterruptedRemediation]:
    """Move every stranded `REMEDIATION_IN_PROGRESS` incident to the truth.

    The status write is a compare-and-set on the old status, so when more than
    one API replica sweeps at the same time exactly one of them claims each
    incident and only that one posts to Slack.
    """
    from sqlalchemy import func, select, update

    from backend import crud, database, models

    now = now or utc_now()
    threshold = threshold if threshold is not None else stale_after()
    recovered: list[InterruptedRemediation] = []

    async with database.AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(models.Incident).where(
                        models.Incident.status
                        == models.IncidentStatus.REMEDIATION_IN_PROGRESS
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return recovered

        candidates: list[tuple[Any, datetime]] = []
        for incident in rows:
            timeline_at = (
                await db.execute(
                    select(func.max(models.IncidentTimelineEvent.created_at)).where(
                        models.IncidentTimelineEvent.incident_id == incident.id
                    )
                )
            ).scalar_one_or_none()
            decided_at = (
                await db.execute(
                    select(func.max(models.ApprovalRequest.decided_at)).where(
                        models.ApprovalRequest.incident_id == incident.id
                    )
                )
            ).scalar_one_or_none()
            manifest_at = (
                await db.execute(
                    select(func.max(models.RunManifest.created_at)).where(
                        models.RunManifest.incident_id == incident.id
                    )
                )
            ).scalar_one_or_none()
            activity = last_activity_at(
                created_at=incident.created_at,
                timeline_at=timeline_at,
                decided_at=decided_at,
                manifest_at=manifest_at,
            )
            if is_interrupted(last_activity=activity, now=now, threshold=threshold):
                candidates.append((incident, activity))

        for incident, activity in candidates:
            claimed = await db.execute(
                update(models.Incident)
                .where(
                    models.Incident.id == incident.id,
                    models.Incident.status
                    == models.IncidentStatus.REMEDIATION_IN_PROGRESS,
                )
                .values(status=models.IncidentStatus.VERIFICATION_UNKNOWN)
            )
            if claimed.rowcount != 1:
                # Another replica got there first, or the run woke up and
                # finished between the scan and the write.
                await db.rollback()
                continue
            await db.commit()

            silent_for = now - activity
            try:
                await crud.create_incident_timeline_event(
                    db,
                    incident.id,
                    event_type="remediation_interrupted",
                    speaker_role="system",
                    title="Remediation interrupted",
                    content=(
                        "The approved remediation stopped reporting after "
                        f"{int(silent_for.total_seconds())}s and its process is "
                        "gone. A cluster change may have been applied but was "
                        "never verified; status moved to verification_unknown."
                    ),
                    payload={
                        "last_activity": activity.isoformat(),
                        "silent_seconds": int(silent_for.total_seconds()),
                        "previous_status": (
                            models.IncidentStatus.REMEDIATION_IN_PROGRESS.value
                        ),
                    },
                )
            except Exception as exc:  # pragma: no cover - never block recovery
                logger.warning(
                    "reconciler: timeline write failed for %s: %s", incident.id, exc
                )

            notified = False
            try:
                from sre_agent.war_room_service import post_to_incident_thread

                notified = await post_to_incident_thread(
                    str(incident.id),
                    interrupted_message(title=incident.title, silent_for=silent_for),
                )
            except Exception as exc:  # pragma: no cover - never block recovery
                logger.warning(
                    "reconciler: Slack notify failed for %s: %s", incident.id, exc
                )
            if not notified:
                # Slack is the only channel this platform has. A recovery the
                # on-call never hears about is only half a recovery, so it is
                # logged loudly rather than silently swallowed.
                logger.error(
                    "reconciler: incident %s recovered to verification_unknown "
                    "but NO Slack notice was delivered",
                    incident.id,
                )

            recovered.append(
                InterruptedRemediation(
                    incident_id=str(incident.id),
                    title=incident.title,
                    last_activity=activity,
                    silent_for=silent_for,
                    new_status=models.IncidentStatus.VERIFICATION_UNKNOWN.value,
                    slack_notified=notified,
                )
            )

    if recovered:
        logger.warning(
            "reconciler: recovered %d interrupted remediation(s): %s",
            len(recovered),
            ", ".join(r.incident_id for r in recovered),
        )
    return recovered


async def reconcile_loop() -> None:
    """Sweep periodically, not only at startup.

    A restart is the common way a remediation dies, but not the only one: the
    driving task can be cancelled, or the request can be abandoned, while the
    process lives on. Those incidents would wait for the next deploy.
    """
    logger.info("Incident reconciler started (every %.0fs)", sweep_interval())
    while not _STOP.is_set():
        try:
            await reconcile_interrupted_remediations()
        except Exception as exc:
            logger.exception("reconciler sweep failed: %s", exc)
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=sweep_interval())
        except asyncio.TimeoutError:
            pass
    logger.info("Incident reconciler stopped")


def start_reconciler() -> Optional[asyncio.Task]:
    global _SWEEP_TASK
    if os.getenv("INCIDENT_RECONCILER_ENABLED", "true").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return None
    _STOP.clear()
    if _SWEEP_TASK and not _SWEEP_TASK.done():
        return _SWEEP_TASK
    _SWEEP_TASK = asyncio.create_task(reconcile_loop(), name="incident-reconciler")
    return _SWEEP_TASK


async def stop_reconciler() -> None:
    _STOP.set()
    task = _SWEEP_TASK
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
