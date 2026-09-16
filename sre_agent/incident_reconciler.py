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

The same silence exists one step *earlier*, before any approval is given, and
is handled here too. `format_approval_request` ends every gate message with
"Expires <t>." but nothing ever fires at that time: every write of
`ApprovalStatus.EXPIRED` in the codebase is reactive, reached only when someone
*tries* to act — a new request for the same incident
(`approval_flow` lines 263/380), or a decision attempted on a dead one
(`approval_flow` 722, `mission_control` 954). With nobody trying, the approval
row stays `pending` forever and the incident stays `AWAITING_APPROVAL` forever.

Alertmanager resolved notifications have a related one-shot delivery gap: if
the control plane is down until an alert expires, the close webhook is lost.
The periodic loop also runs the fail-closed Prometheus rule-state reconciler;
that path requires two healthy snapshots and treats them only as lifecycle
evidence, never as proof that remediation succeeded.

Observed live on 2026-09-14: five approval requests sat `pending` hours past
`expires_at` — d3ca5138 among them, whose window closed at 07:51:17Z and which
was still `awaiting_approval` at 10:39Z with no message after the one telling
the on-call when it would lapse.

Safety was never the issue: `war_room` rejects a late `approve fix` as
"expired" on the timestamp, regardless of the stored status, so nothing can run
after the window. What was broken is that Slack — the only channel this
platform has — announced the offer and never announced its death. A human who
stepped away could not tell from the thread whether the fix had run, was still
waiting, or had quietly lapsed. `reconcile_lapsed_approvals` retires the row,
moves the incident to `INVESTIGATED` ("investigated, nothing was authorized,
nothing ran") and says so in the thread.
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


@dataclass(frozen=True)
class LapsedApproval:
    """One approval offer this sweep retired because its window closed."""

    incident_id: str
    title: str
    expires_at: datetime
    lapsed_for: timedelta
    new_status: str
    slack_notified: bool


def lapsed_message(*, title: str, lapsed_for: timedelta) -> str:
    """What the on-call reads when the window closes with no answer.

    States the one thing that is unambiguously true — nothing ran — because
    the failure this repairs is a human unable to tell a silent success from a
    silent lapse.
    """
    minutes = int(lapsed_for.total_seconds() // 60)
    return (
        ":hourglass: *Approval window closed*\n"
        f"The approval requested for *{title}* expired {minutes} minute(s) ago "
        "with no reply, so *nothing was run* — the cluster is unchanged and the "
        "problem is still open.\n"
        "`approve fix` will no longer be accepted on this thread. Status moved "
        "to `investigated`. There is no way to re-run the investigation on "
        "this incident — while it stays open, dedup folds the re-firing alert "
        "into it. Fix it by hand, or reply `mark resolved` to close it so the "
        "next firing alert opens a fresh incident and a fresh approval."
    )


async def reconcile_lapsed_approvals(
    *, now: Optional[datetime] = None
) -> list[LapsedApproval]:
    """Retire every approval offer whose deadline passed with no decision.

    Claimed with a compare-and-set on the *approval* row rather than the
    incident, because the approval is the authorization-bearing record and the
    one a second replica must not also retire. Marking it `EXPIRED` cannot
    widen what is permitted: `war_room` already refuses a late `approve fix` by
    comparing `expires_at` to the clock, so this only makes the stored state
    agree with the answer the handler was giving all along.

    Every expired row is retired regardless of its incident's status — an
    offer past its deadline is dead whatever happened around it, and live runs
    left `pending` rows behind on incidents that had already moved on
    (2c49ac9d was `resolved` with one still open). Moving the incident and
    telling Slack are the narrower steps: both are reserved for an incident
    still sitting in `AWAITING_APPROVAL` with no other live offer, which is
    the only case where a human is actually waiting on an answer.
    """
    from sqlalchemy import select, update

    from backend import crud, database, models

    now = now or utc_now()
    lapsed: list[LapsedApproval] = []

    async with database.AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(models.ApprovalRequest, models.Incident)
                .join(
                    models.Incident,
                    models.Incident.id == models.ApprovalRequest.incident_id,
                )
                .where(
                    models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
                    models.ApprovalRequest.expires_at <= now,
                )
            )
        ).all()

        for request, incident in rows:
            claimed = await db.execute(
                update(models.ApprovalRequest)
                .where(
                    models.ApprovalRequest.id == request.id,
                    models.ApprovalRequest.status == models.ApprovalStatus.PENDING,
                )
                .values(status=models.ApprovalStatus.EXPIRED, decided_at=now)
            )
            if claimed.rowcount != 1:
                # Another replica retired it, or a human decided it in the
                # moment between the scan and this write.
                await db.rollback()
                continue

            # Only move the incident when this was its last live offer. A
            # newer pending request means the incident is legitimately still
            # waiting on a human and must stay AWAITING_APPROVAL.
            still_open = (
                await db.execute(
                    select(models.ApprovalRequest.id)
                    .where(
                        models.ApprovalRequest.incident_id == incident.id,
                        models.ApprovalRequest.status
                        == models.ApprovalStatus.PENDING,
                    )
                    .limit(1)
                )
            ).first()
            moved = False
            if still_open is None:
                status_write = await db.execute(
                    update(models.Incident)
                    .where(
                        models.Incident.id == incident.id,
                        models.Incident.status
                        == models.IncidentStatus.AWAITING_APPROVAL,
                    )
                    .values(status=models.IncidentStatus.INVESTIGATED)
                )
                moved = status_write.rowcount == 1
            await db.commit()

            expires_at = _as_aware(request.expires_at) or now
            lapsed_for = now - expires_at

            if not moved:
                # The row is retired either way, but with the incident left
                # where it was there is nothing new to tell the on-call.
                continue

            try:
                await crud.create_incident_timeline_event(
                    db,
                    incident.id,
                    event_type="approval_expired",
                    speaker_role="system",
                    title="Approval window closed",
                    content=(
                        "The approval request expired after "
                        f"{int(lapsed_for.total_seconds())}s with no decision. "
                        "No action was executed; status moved to investigated."
                    ),
                    payload={
                        "approval_request_id": str(request.id),
                        "expires_at": expires_at.isoformat(),
                        "lapsed_seconds": int(lapsed_for.total_seconds()),
                        "previous_status": (
                            models.IncidentStatus.AWAITING_APPROVAL.value
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
                    lapsed_message(title=incident.title, lapsed_for=lapsed_for),
                )
            except Exception as exc:  # pragma: no cover - never block recovery
                logger.warning(
                    "reconciler: Slack notify failed for %s: %s", incident.id, exc
                )
            if not notified:
                # The entire point of this sweep is the Slack message; a lapse
                # nobody is told about is the bug it was written to fix.
                logger.error(
                    "reconciler: approval for incident %s lapsed but NO Slack "
                    "notice was delivered",
                    incident.id,
                )

            lapsed.append(
                LapsedApproval(
                    incident_id=str(incident.id),
                    title=incident.title,
                    expires_at=expires_at,
                    lapsed_for=lapsed_for,
                    new_status=models.IncidentStatus.INVESTIGATED.value,
                    slack_notified=notified,
                )
            )

    if lapsed:
        logger.warning(
            "reconciler: retired %d lapsed approval(s): %s",
            len(lapsed),
            ", ".join(r.incident_id for r in lapsed),
        )
    return lapsed


async def reconcile_loop() -> None:
    """Sweep periodically, not only at startup.

    A restart is the common way a remediation dies, but not the only one: the
    driving task can be cancelled, or the request can be abandoned, while the
    process lives on. Those incidents would wait for the next deploy. An
    approval deadline is a clock, not an event, so it can only ever be noticed
    by a sweep like this one.
    """
    logger.info("Incident reconciler started (every %.0fs)", sweep_interval())
    while not _STOP.is_set():
        try:
            await reconcile_interrupted_remediations()
        except Exception as exc:
            logger.exception("reconciler sweep failed: %s", exc)
        try:
            # Kept separate so a failure in one sweep cannot silence the other.
            await reconcile_lapsed_approvals()
        except Exception as exc:
            logger.exception("reconciler: lapsed-approval sweep failed: %s", exc)
        try:
            # A successful empty metric query is not enough: the dedicated
            # reconciler checks rule existence/health twice with a durable
            # observation between checks before synthesizing a missed clear.
            from sre_agent.alert_lifecycle_reconciler import (
                reconcile_missed_alert_resolutions,
            )

            await reconcile_missed_alert_resolutions()
        except Exception as exc:
            logger.exception("reconciler: missed-alert-clear sweep failed: %s", exc)
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
