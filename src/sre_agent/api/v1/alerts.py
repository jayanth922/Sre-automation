"""
Alert Webhook Router — Receives Alertmanager webhooks and creates incidents.

Flow: Alertmanager fires alert → POST /api/v1/alerts/webhook → create incident
      → trigger background SRE Agent investigation.

Resolved notifications correlate to the active incident and apply verification
rules (never masking ``REMEDIATION_FAILED``).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, database, models, schemas
from sre_agent import job_store
from sre_agent.alert_resolution import reconcile_resolved_alert
from sre_agent.incident_correlation import (
    CorrelationCandidate,
    actionable_bundle,
    correlate,
    extract_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/alerts",
    tags=["alerts"],
)


# ---------------------------------------------------------------------------
# Auth: reuse cluster-token authentication from agent_connect
# ---------------------------------------------------------------------------

async def _get_cluster_from_token(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(database.get_db),
) -> models.Cluster:
    """Authenticate via cluster token sent by Alertmanager's http_config."""
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("Webhook rejected: Missing or invalid Authorization header")
        raise HTTPException(status_code=403, detail="Missing or invalid cluster token")

    token = authorization.split(" ", 1)[1]
    cluster = await crud.get_cluster_by_token(db, token)
    if not cluster:
        logger.warning("Webhook rejected: Invalid cluster token provided")
        raise HTTPException(status_code=403, detail="Invalid cluster token")
    return cluster


# ---------------------------------------------------------------------------
# Helpers: parse Alertmanager payload
# ---------------------------------------------------------------------------

# Map Alertmanager severity labels → our IncidentSeverity enum
_SEVERITY_MAP = {
    "critical": models.IncidentSeverity.CRITICAL,
    "high":     models.IncidentSeverity.HIGH,
    "warning":  models.IncidentSeverity.MEDIUM,
    "info":     models.IncidentSeverity.LOW,
}


def _parse_alertmanager_payload(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract a flat list of alert dicts from the Alertmanager webhook body."""
    parsed: List[Dict[str, Any]] = []
    for alert in body.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        parsed.append({
            "status":      alert.get("status", "firing"),
            "alertname":   labels.get("alertname", "UnknownAlert"),
            "severity":    labels.get("severity", "warning"),
            "service":     labels.get("service", "unknown"),
            "summary":     annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "starts_at":   alert.get("startsAt", ""),
            "ends_at":     alert.get("endsAt", ""),
            "labels":      labels,
        })
    return parsed


def _incident_title(alert: Dict[str, Any]) -> str:
    return f"[{alert['service']}] {alert['alertname']}"


def _status_str(status: Any) -> Optional[str]:
    if status is None:
        return None
    return str(getattr(status, "value", status))


def _plain(text: str) -> str:
    """Drop Slack's `*bold*` markers so the same phrase reads in the dashboard."""
    return text.replace("*", "")


# ---------------------------------------------------------------------------
# A re-firing alert that lands on a parked incident
# ---------------------------------------------------------------------------
#
# Dedup collapses a re-firing alert into the incident already tracking it for
# as long as that incident is non-resolved, which is right: one condition, one
# war room. But "non-resolved" covers two very different situations, and the
# dedup branch treated them the same — log a line, drop the alert.
#
# In OPEN / INVESTIGATING / REMEDIATION_IN_PROGRESS something is running, and
# in AWAITING_APPROVAL a question is already sitting in front of a human. There
# the silence is correct; repeating ourselves would be noise.
#
# The statuses below are the other kind. Nothing is running on the incident and
# nobody has been asked anything, so no future event will move it: the alert is
# swallowed, and it will be swallowed again every group interval, forever.
#
# Observed live on 2026-09-14T11:08:00Z. `d3ca5138` investigated a checkout
# memory leak, asked for approval, and got no reply; the lapse sweep retired
# the request, moved it to `investigated`, and told the thread — accurately —
# "the problem is still open ... to act on it now, a human has to take it from
# here or re-run the investigation to raise a fresh approval." The alert then
# re-fired and the dedup branch discarded it with a log line nobody reads. The
# system knew the condition was live, had an open Slack thread for it, and said
# nothing. Slack is the only channel this product has, so a parked incident
# quietly absorbing its own alert is the channel failing.
_PARKED_INCIDENT_STATUSES = frozenset({
    models.IncidentStatus.INVESTIGATED,
    models.IncidentStatus.REMEDIATION_FAILED,
    models.IncidentStatus.VERIFICATION_UNKNOWN,
    models.IncidentStatus.PENDING_ACKNOWLEDGMENT,
})

# The statuses above are parked by definition. These two are parked *or* busy,
# and the status alone cannot tell you which — only the job table can.
#
# `OPEN` is written twice with opposite meanings: once by `create_incident`, a
# beat before the investigation job is enqueued, and again by the failure path
# in `agent_runtime` when a run dies. `INVESTIGATING` is written at the start
# of a run and never unwound if the worker dies before recording an outcome.
# In the dead cases the incident reads as live, no job exists, and every later
# firing of the alert dedups into it and is discarded.
#
# Observed live on 2026-09-14. Incident `3b879513` ([pdf-thumbnailer]
# PodOOMKilled) opened at 12:37; its investigation died two seconds later on a
# transient LLM credit error and the failure path wrote the incident back to
# `open`. Over the next six hours the alert re-fired at least thirteen times,
# each one logged as `Dedup: ... already open as incident 3b879513` and thrown
# away. The incident still has exactly one timeline event and its Slack thread
# was never told anything. A pod was OOMKilling all afternoon on the only
# channel this product has.
_CONDITIONALLY_PARKED_STATUSES = frozenset({
    models.IncidentStatus.OPEN,
    models.IncidentStatus.INVESTIGATING,
})

# A newly created incident is `OPEN` for the few milliseconds between
# `create_incident` committing and `enqueue_and_kick` committing its job. A
# concurrent delivery landing inside that window would find no live job and
# announce "nothing is working on it" about an incident that is about to be
# investigated. Alertmanager's group_interval is around a minute, so this is
# nearly unreachable, but the notice is a loud one and being wrong on it costs
# more than being a minute late.
_INVESTIGATION_START_GRACE_SECONDS = 120

# Alertmanager redelivers a firing group every group_interval (about once a
# minute here), so the notice needs a floor or the thread becomes a metronome.
# It is deliberately a repeat rather than a one-shot: an unattended incident
# whose alert is still firing an hour later is worth saying again.
_REFIRE_NOTICE_COOLDOWN_MINUTES = 60

_REFIRE_EVENT_TYPE = "alert_refired"

# What each parked status means for someone reading the thread cold. Phrased so
# the reader learns what did *not* happen, which is the part the status name
# hides.
_PARKED_STATUS_MEANING = {
    models.IncidentStatus.INVESTIGATED: (
        "the investigation finished and *nothing was changed on the cluster*"
    ),
    models.IncidentStatus.REMEDIATION_FAILED: (
        "the last remediation attempt *failed*, so the cluster was not fixed"
    ),
    models.IncidentStatus.VERIFICATION_UNKNOWN: (
        "a remediation was started and its outcome was *never confirmed*"
    ),
    models.IncidentStatus.PENDING_ACKNOWLEDGMENT: (
        "a fix was applied and verified — this alert coming back means it "
        "*did not hold*"
    ),
}

# The two conditional statuses, once the job table has ruled out live work.
# Both say the quiet part: the status names an activity that is not happening.
_DEAD_INVESTIGATION_MEANING = {
    # These land inside an em-dash clause in `refire_message`, so they punctuate
    # with a colon; a second dash reads as a stutter in the thread.
    models.IncidentStatus.OPEN: (
        "*no investigation is queued or running for it*: the one that started "
        "died before it reached a conclusion, or never started at all"
    ),
    models.IncidentStatus.INVESTIGATING: (
        "it is *labelled* as under investigation but *no investigation job "
        "exists*: the run died without recording an outcome, so the label is "
        "stale"
    ),
}


async def _parked_meaning(
    db: AsyncSession, incident: models.Incident, *, now: datetime
) -> Optional[str]:
    """Why this incident is going nowhere, or None if something is working it.

    Returning None is the safe answer: it means the alert is silently deduped,
    which is correct whenever a run really is in flight and merely noisy to get
    wrong in the other direction. So every uncertainty here — a failed lookup,
    a missing timestamp — resolves to None rather than to a loud claim that
    nobody is on it.
    """
    status = incident.status
    if status in _PARKED_INCIDENT_STATUSES:
        return _PARKED_STATUS_MEANING.get(
            status, "no work is in progress on it and nobody has been asked anything"
        )
    if status not in _CONDITIONALLY_PARKED_STATUSES:
        return None

    if status == models.IncidentStatus.OPEN:
        created = getattr(incident, "created_at", None)
        if created is None:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now - created < timedelta(seconds=_INVESTIGATION_START_GRACE_SECONDS):
            return None

    try:
        if await job_store.has_live_investigation_job(db, incident.id):
            return None
    except Exception as exc:  # pragma: no cover - never block the webhook
        logger.warning(
            "refire notice: live-job lookup failed for %s: %s", incident.id, exc
        )
        return None
    return _DEAD_INVESTIGATION_MEANING[status]


def refire_message(*, status: Any, title: str, meaning: str) -> str:
    """The Slack notice for an alert that re-fired onto a parked incident.

    `title` is `[service] AlertName`, so it already contains the alert name —
    naming both reads as a stutter ("`CheckoutMemoryApproachingLimit` fired
    again. It belongs to *[checkout-service] CheckoutMemoryApproachingLimit*"),
    which is how the first live notice read. The title alone carries both.

    `meaning` comes from `_parked_meaning` rather than from `status`, because
    for `open` and `investigating` the status name and the truth disagree.
    """
    return (
        ":rotating_light: *Still firing, and nothing is working on it*\n"
        f"*{title}* is firing again, and its incident is sitting at "
        f"`{_status_str(status)}` — {meaning}.\n"
        "Sentinel will not open a second incident while this one is here, and "
        "it will not re-investigate on its own — there is no \"investigate "
        "again\" command. This alert has nowhere else to go. Reply "
        "`mark resolved` to close this incident and the next firing alert "
        "opens a fresh one with a fresh investigation, or fix it by hand.\n"
        f"_Repeated at most once every {_REFIRE_NOTICE_COOLDOWN_MINUTES} "
        "minutes while this stays true._"
    )


async def _recently_announced_refire(
    db: AsyncSession, incident_id: Any, *, now: datetime
) -> bool:
    """True if this incident's thread was already told inside the cooldown."""
    result = await db.execute(
        select(models.IncidentTimelineEvent.created_at)
        .filter(
            models.IncidentTimelineEvent.incident_id == incident_id,
            models.IncidentTimelineEvent.event_type == _REFIRE_EVENT_TYPE,
        )
        .order_by(models.IncidentTimelineEvent.created_at.desc())
        .limit(1)
    )
    last = result.scalars().first()
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(minutes=_REFIRE_NOTICE_COOLDOWN_MINUTES)


async def _announce_refire_on_parked_incident(
    db: AsyncSession,
    incident: models.Incident,
    alert: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Tell the incident's thread that its alert is firing again.

    Returns True when a notice was posted. Never raises: a webhook that 500s
    makes Alertmanager retry the whole group, and losing the alert entirely is
    worse than losing the notice.
    """
    now = now or datetime.now(timezone.utc)
    meaning = await _parked_meaning(db, incident, now=now)
    if meaning is None:
        return False
    try:
        if await _recently_announced_refire(db, incident.id, now=now):
            return False
    except Exception as exc:  # pragma: no cover - never block the webhook
        logger.warning(
            "refire notice: cooldown lookup failed for %s: %s", incident.id, exc
        )
        return False

    message = refire_message(
        status=incident.status,
        title=incident.title,
        meaning=meaning,
    )

    # Written before the post so the cooldown holds even if Slack is down —
    # otherwise a broken Slack turns this into a retry storm against Slack.
    try:
        await crud.create_incident_timeline_event(
            db,
            incident.id,
            event_type=_REFIRE_EVENT_TYPE,
            speaker_role="system",
            title="Alert fired again on a parked incident",
            content=(
                f"{alert['alertname']} is firing again while this incident sits "
                f"at {_status_str(incident.status)} — {_plain(meaning)}. No "
                "investigation or remediation is in progress and no approval "
                "is outstanding."
            ),
            payload={
                "alertname": alert["alertname"],
                "incident_status": _status_str(incident.status),
                "parked_reason": _plain(meaning),
                "labels": alert.get("labels") or {},
            },
        )
    except Exception as exc:  # pragma: no cover - never block the webhook
        logger.warning(
            "refire notice: timeline write failed for %s: %s", incident.id, exc
        )
        return False

    notified = False
    try:
        from sre_agent.war_room_service import post_to_incident_thread

        notified = await post_to_incident_thread(str(incident.id), message)
    except Exception as exc:  # pragma: no cover - never block the webhook
        logger.warning(
            "refire notice: Slack notify failed for %s: %s", incident.id, exc
        )
    if not notified:
        # The Slack message is the entire point; a re-fire nobody is told about
        # is the bug this exists to fix.
        logger.error(
            "refire notice: '%s' re-fired on parked incident %s but NO Slack "
            "notice was delivered",
            alert["alertname"],
            incident.id,
        )
    return notified


async def _record_correlation_shadow(
    db: AsyncSession,
    cluster: models.Cluster,
    incident: models.Incident,
) -> None:
    """Phase 5 correlation gate, shadow mode (docs/ai/PHASE5_DETERMINISTIC_PIPELINE_PLAN.md,
    Phase A): score the just-created incident against every other open incident
    in its cluster and record what it *would* bundle with, purely as an
    observational timeline event. Never changes dedup, investigation, or
    execution behavior — this is validation data before the correlation gate
    is allowed to act on anything.
    """
    open_incidents = await crud.list_active_incidents_for_cluster(
        db, cluster.id, exclude_incident_id=incident.id
    )
    if not open_incidents:
        return

    candidate = CorrelationCandidate(
        incident_id=str(incident.id),
        cluster_id=str(cluster.id),
        title=incident.title,
        description=incident.description or "",
        created_at=incident.created_at,
    )
    pool = [
        CorrelationCandidate(
            incident_id=str(other.id),
            cluster_id=str(cluster.id),
            title=other.title,
            description=other.description or "",
            created_at=other.created_at,
        )
        for other in open_incidents
    ]
    adjacency = None
    try:
        from sre_agent.service_topology import get_adjacency_map

        adjacency = await get_adjacency_map(cluster)
    except Exception as e:
        logger.warning(f"Correlation shadow: adjacency fetch failed for cluster {cluster.id}: {e}")

    result = correlate(candidate, pool, adjacency=adjacency)
    if result.decision != "bundle":
        return

    await crud.create_incident_timeline_event(
        db,
        incident.id,
        event_type="correlation_shadow",
        speaker_role="system",
        title="Correlation gate (shadow mode)",
        content=(
            f"Would bundle with incident {result.bundle_with} "
            f"(score={result.best_score:.2f}): {'; '.join(result.matches[0].reasons)}"
        ),
        payload={
            "source": "incident_correlation",
            "mode": "shadow",
            "bundle_with": result.bundle_with,
            "score": result.best_score,
            "matches": [
                {"incident_id": m.incident_id, "score": m.score, "reasons": m.reasons}
                for m in result.matches
            ],
        },
    )
    logger.info(
        "Correlation shadow: incident %s would bundle with %s (score=%.2f)",
        incident.id,
        result.bundle_with,
        result.best_score,
    )


_FOLD_EVENT_TYPE = "correlated_alert_folded"

# `correlate` wants an incident id to exclude itself from its own pool. The
# fold decision is made *before* the row exists — that is the whole point, so
# that a duplicate never becomes a second war room — so it gets a sentinel no
# real incident can collide with.
_UNCREATED_INCIDENT_ID = "uncreated"

# `DEFAULT_WINDOW_MINUTES` is 15, and it is the right default for *scoring*:
# two alerts firing far apart are weak evidence of one fault. It is the wrong
# bound for *folding*, because the fold pool is already every open incident
# that something is actively working — being still open and still worked is a
# stronger recency signal than an age in minutes. Live, `3ed8be00` opened at
# 01:02 and its duplicate `88fa9ee4` arrived at 01:17: exactly 15 minutes,
# admitted by a hair. A remediation cycle with human approval and a 180s
# verification routinely runs longer than that, and the two later
# pdf-thumbnailer threads fell outside it entirely. Two hours bounds a fold to
# roughly one on-call's working context without letting the clock alone
# reintroduce the duplicate threads this exists to stop.
_FOLD_WINDOW_MINUTES = 120


def fold_message(*, folded_title: str, parent_title: str, service: str) -> str:
    """The Slack notice for an alert folded into an already-open incident.

    States the two things the reader cannot see from the thread: that a second
    alert is now firing, and that nothing separate will be done about it. The
    second is the part that has to be said out loud — silently absorbing an
    alert is the failure mode this whole path has to avoid.
    """
    return (
        ":link: *A second alert on this service — folded in here*\n"
        f"*{folded_title}* is now firing too. It is the same service "
        f"(`{service}`) as this incident, which is almost always one fault "
        "showing up twice — a pod that gets OOMKilled is also a pod that "
        "crashloops.\n"
        "So it was folded into this thread instead of opening a second one, "
        "and *no separate investigation will run for it* — the work already "
        f"in progress on *{parent_title}* is what covers it.\n"
        "If it turns out to be its own problem, reply `mark resolved` to "
        "close this incident; the next firing alert then opens a fresh one "
        "with its own investigation."
    )


async def _find_fold_target(
    db: AsyncSession,
    cluster: models.Cluster,
    title: str,
    description: str,
    *,
    now: datetime,
) -> Optional[models.Incident]:
    """The open incident this alert should fold into, or None to open its own.

    Three conditions, each of which has to hold:

    1. `actionable_bundle` says same service — the only correlation shadow
       mode validated (12/12; see its docstring for the three cross-service
       bundles it got wrong).
    2. Something is actually working the parent. `_parked_meaning` returning
       non-None means nothing is, and folding onto a parked incident would
       mean nothing works the *folded* alert either — we would have suppressed
       the one mechanism that investigates it. A different title is a real
       choice, unlike exact-title dedup, so it gets made the safe way.
    3. The parent has a Slack thread. Slack is the only channel this platform
       talks over; a fold with nowhere to announce itself is an alert that
       silently disappears.
    """
    open_incidents = await crud.list_active_incidents_for_cluster(db, cluster.id)
    if not open_incidents:
        return None

    candidate = CorrelationCandidate(
        incident_id=_UNCREATED_INCIDENT_ID,
        cluster_id=str(cluster.id),
        title=title,
        description=description,
        created_at=now,
    )
    pool = [
        CorrelationCandidate(
            incident_id=str(other.id),
            cluster_id=str(cluster.id),
            title=other.title,
            description=other.description or "",
            created_at=other.created_at,
        )
        for other in open_incidents
    ]
    match = actionable_bundle(candidate, pool, window_minutes=_FOLD_WINDOW_MINUTES)
    if match is None:
        return None

    parent = next((i for i in open_incidents if str(i.id) == match.incident_id), None)
    if parent is None:  # pragma: no cover - the pool is built from this list
        return None

    if await _parked_meaning(db, parent, now=now) is not None:
        logger.info(
            "Fold declined: '%s' matches parked incident %s (%s); opening its own",
            title,
            parent.id,
            _status_str(parent.status),
        )
        return None

    if not (parent.slack_channel and parent.slack_thread_ts):
        logger.info(
            "Fold declined: '%s' matches incident %s but it has no Slack thread "
            "to fold into; opening its own",
            title,
            parent.id,
        )
        return None

    return parent


async def _fold_alert_into_incident(
    db: AsyncSession,
    parent: models.Incident,
    alert: Dict[str, Any],
    title: str,
    service: str,
) -> bool:
    """Absorb a correlated alert into `parent`. True if it was actually folded.

    The Slack notice is posted *first* and the fold is conditional on it
    landing. That inverts the usual write-then-notify order on purpose: a fold
    the on-call cannot see is strictly worse than the extra thread it saves
    them. The complaint this path answers is "too many incidents and I cannot
    find them in Slack" — an alert that quietly vanishes answers it the wrong
    way. If the notice does not land, the caller opens the incident normally
    and the duplicate thread is the acceptable outcome.

    Never raises: a webhook that 500s makes Alertmanager retry the whole group.
    """
    message = fold_message(
        folded_title=title, parent_title=parent.title, service=service
    )
    delivered = False
    try:
        from sre_agent.war_room_service import post_to_incident_thread

        delivered = await post_to_incident_thread(str(parent.id), message)
    except Exception as exc:  # pragma: no cover - never block the webhook
        logger.warning("fold: Slack notify failed for %s: %s", parent.id, exc)

    if not delivered:
        logger.warning(
            "fold: '%s' correlates with incident %s but the Slack notice did "
            "not land — opening its own incident instead",
            title,
            parent.id,
        )
        return False

    try:
        await crud.create_incident_timeline_event(
            db,
            parent.id,
            event_type=_FOLD_EVENT_TYPE,
            speaker_role="system",
            title="Correlated alert folded into this incident",
            content=(
                f"{title} started firing on the same service and was folded "
                f"into this incident. No separate incident, war room, or "
                f"investigation was created for it."
            ),
            payload={
                "source": "incident_correlation",
                "mode": "acting",
                "folded_title": title,
                "alertname": alert["alertname"],
                "labels": alert.get("labels") or {},
            },
        )
    except Exception as exc:  # pragma: no cover - never block the webhook
        # The thread has already been told this alert is folded. Opening an
        # incident now would contradict a message the on-call can read, so the
        # fold stands and the audit row is what was lost.
        logger.warning("fold: timeline write failed for %s: %s", parent.id, exc)
        await db.rollback()
    return True


async def _reconcile_resolved_alert(
    db: AsyncSession,
    cluster: models.Cluster,
    alert: Dict[str, Any],
    *,
    reconciliation_source: str = "alertmanager_webhook",
) -> Dict[str, Any]:
    """Correlate a resolved alert to an active incident and apply status rules."""
    title = _incident_title(alert)
    incident = await crud.find_active_incident_by_title(db, cluster.id, title)
    if not incident:
        logger.info("Resolved alert '%s' had no active incident to reconcile", alert["alertname"])
        return {
            "alertname": alert["alertname"],
            "matched": False,
            "reason": "no_active_incident",
        }

    decision = reconcile_resolved_alert(incident.status)
    values: Dict[str, Any] = {}
    if decision.mark_resolved and decision.new_status is not None:
        values["status"] = models.IncidentStatus(decision.new_status)
        values["resolved_at"] = datetime.now(timezone.utc)
    elif (
        decision.new_status is not None
        and decision.new_status != _status_str(incident.status)
    ):
        values["status"] = models.IncidentStatus(decision.new_status)

    if values:
        claimed = await db.execute(
            update(models.Incident)
            .where(
                models.Incident.id == incident.id,
                models.Incident.status == incident.status,
            )
            .values(**values)
        )
        # Two API replicas can receive the same resolved group, and the
        # background missed-clear sweep can race a late webhook. Only the
        # compare-and-set winner may withdraw approvals and announce closure.
        if getattr(claimed, "rowcount", 1) != 1:
            await db.rollback()
            return {
                "alertname": alert["alertname"],
                "incident_id": str(incident.id),
                "matched": False,
                "reason": "status_changed_concurrently",
            }
        await db.commit()

    # External recovery is not a human stop command. Keep the evidence-gathering
    # job alive so it can finish and post findings, but retire all remediation
    # authority and close the incident's interactive lifecycle.
    if decision.mark_resolved:
        try:
            from sre_agent.approval_flow import (
                fire_external_alert_clear_side_effects,
            )

            if reconciliation_source == "prometheus_rule_recovery":
                await fire_external_alert_clear_side_effects(
                    incident,
                    str(cluster.org_id),
                    str(cluster.id),
                    source_label="Prometheus rule reconciliation",
                )
            else:
                await fire_external_alert_clear_side_effects(
                    incident, str(cluster.org_id), str(cluster.id)
                )
        except Exception as side_effect_err:
            logger.warning(
                "Resolution side effects failed for incident %s: %s",
                incident.id,
                side_effect_err,
            )

    await crud.create_incident_timeline_event(
        db,
        incident.id,
        event_type="alert_resolved",
        speaker_role="system",
        title=(
            "Prometheus missed-clear reconciliation"
            if reconciliation_source == "prometheus_rule_recovery"
            else "Alertmanager resolved notification"
        ),
        content=(
            (
                f"Two healthy Prometheus rule snapshots confirmed no active "
                f"series for `{alert['alertname']}` after a missed resolved "
                "notification. "
                if reconciliation_source == "prometheus_rule_recovery"
                else f"Alert `{alert['alertname']}` cleared externally. "
            )
            + f"{decision.reason}."
            + (
                " Remediation failure preserved; incident not marked resolved."
                if decision.masked_failed_remediation
                else ""
            )
            + " This closes the source-alert lifecycle only; it does not prove "
            "that a Sentinel remediation succeeded."
        ),
        payload={
            "alertname": alert["alertname"],
            "previous_status": str(decision.previous_status) if decision.previous_status else None,
            "new_status": str(decision.new_status) if decision.new_status else None,
            "mark_resolved": decision.mark_resolved,
            "masked_failed_remediation": decision.masked_failed_remediation,
            "ends_at": alert.get("ends_at") or None,
            "labels": alert.get("labels") or {},
            "reconciliation_source": reconciliation_source,
            "remediation_verified": False,
        },
    )
    logger.info(
        "Reconciled resolved alert '%s' → incident %s (%s → %s, mark_resolved=%s)",
        alert["alertname"],
        incident.id,
        decision.previous_status,
        decision.new_status,
        decision.mark_resolved,
    )
    return {
        "alertname": alert["alertname"],
        "incident_id": str(incident.id),
        "matched": True,
        "previous_status": str(decision.previous_status) if decision.previous_status else None,
        "new_status": str(decision.new_status) if decision.new_status else None,
        "mark_resolved": decision.mark_resolved,
        "masked_failed_remediation": decision.masked_failed_remediation,
        "reason": decision.reason,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def receive_alertmanager_webhook(
    request: Request,
    cluster: models.Cluster = Depends(_get_cluster_from_token),
    db: AsyncSession = Depends(database.get_db),
):
    """
    Receive an Alertmanager webhook, create incidents, and trigger investigations.

    Expected payload (standard Alertmanager v4 format):
    {
      "status": "firing",
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "...", "severity": "critical", "service": "..."},
          "annotations": {"summary": "...", "description": "..."},
          "startsAt": "2026-..."
        }
      ]
    }
    """
    body = await request.json()
    alerts = _parse_alertmanager_payload(body)

    # Authenticating with the cluster token and delivering a webhook is observed
    # Alertmanager connectivity evidence, even when every alert is deduped.
    await crud.update_cluster_heartbeat(
        db,
        cluster.id,
        source="alertmanager",
        reason="alertmanager_webhook",
    )

    if not alerts:
        return {
            "received": 0,
            "incidents_created": 0,
            "resolved_reconciled": 0,
            "detail": "No alerts in payload",
        }

    incidents_created = 0
    incidents_folded = 0
    resolved_reconciled = 0
    reconciliations: List[Dict[str, Any]] = []

    # An Alertmanager group is delivered whole, and a rule that emits one series
    # per label value (InventorySlowQueries emits seven, one per `query`) clears
    # those series a few scrape intervals apart. The notification that carries
    # the first resolved series therefore still carries the ones that are
    # firing, and both kinds land in this single loop.
    #
    # Reconciling a resolved member while a sibling is still firing closes the
    # incident that is actively tracking the condition, and the next firing
    # member in the *same payload* then finds nothing to dedup against and opens
    # a fresh one. Observed live on 2026-09-14T01:38:15 — inside one request:
    # two members deduped onto incident 030d0ffb, a resolved member closed it,
    # the next firing member opened b6146c86, a resolved member closed that, and
    # the last firing member opened 9258aadb. Two orphan war rooms, two durable
    # investigation jobs, two full LLM investigations, and the real incident
    # marked resolved while its remediation was still being verified.
    #
    # One incident tracks one title, so the condition behind that title is
    # cleared only when no member of the payload still reports it firing.
    firing_titles = {
        _incident_title(alert) for alert in alerts if alert["status"] == "firing"
    }

    for alert in alerts:
        if alert["status"] != "firing":
            title = _incident_title(alert)
            if title in firing_titles:
                logger.info(
                    "Resolved alert '%s' ignored: another series of '%s' in the "
                    "same payload is still firing",
                    alert["alertname"],
                    title,
                )
                reconciliations.append({
                    "alertname": alert["alertname"],
                    "matched": False,
                    "reason": "sibling_series_still_firing",
                })
                continue
            result = await _reconcile_resolved_alert(db, cluster, alert)
            reconciliations.append(result)
            if result.get("matched"):
                resolved_reconciled += 1
            continue

        title = _incident_title(alert)
        description = (
            f"{alert['summary']}\n\n{alert['description']}\n\n"
            f"Labels: {json.dumps(alert['labels'], indent=2)}"
        )
        severity = _SEVERITY_MAP.get(alert["severity"], models.IncidentSeverity.MEDIUM)

        # Deduplicate: skip if an open incident with same title exists.
        # Locked first — the lookup and the insert below are a check-then-
        # create, and concurrent deliveries of the same alert group each found
        # nothing and opened their own war room (see
        # crud.lock_incident_dedup). create_incident's commit releases it.
        await crud.lock_incident_dedup(db, cluster.id, title)
        existing = await crud.find_duplicate_incident(db, cluster.id, title)
        if existing:
            logger.info(f"Dedup: '{title}' already open as incident {existing.id}")
            # End the transaction so the dedup lock isn't held for the rest of
            # the request. Commit, not rollback: this transaction only took the
            # lock and ran a SELECT, and the session is `expire_on_commit=False`
            # (src/backend/database.py) while rollback expires every loaded object
            # unconditionally. Rolling back here expired `cluster`, so the next
            # alert in the group hit `cluster.id` and SQLAlchemy tried to
            # refresh it with lazy sync IO — MissingGreenlet, a 500, and
            # Alertmanager retrying the whole group ten times before giving up.
            # An Alertmanager group carries every firing series (seven for this
            # rule), so a payload that is entirely duplicates is the norm, not
            # the edge case.
            await db.commit()
            # Done after the commit so the dedup lock is not held across a
            # Slack round trip. Most dedups land on an incident that is being
            # worked and say nothing; see `_parked_meaning` for the ones that
            # do, including the `open`/`investigating` ones whose status lies.
            await _announce_refire_on_parked_incident(db, existing, alert)
            continue

        # Exact-title dedup missed, which does not mean this is a new problem.
        # `[pdf-thumbnailer] PodOOMKilled` and `[pdf-thumbnailer]
        # PodCrashLooping` are two titles for one pod dying, and each opened
        # its own incident, war room, investigation and remediation — six
        # pdf-thumbnailer threads in 75 minutes on 2026-09-15, which is what
        # "there are too many incidents and i cannot find them correctly in
        # slack" is describing. The correlation gate ran in shadow mode
        # through all of it and scored the duplicate at 1.00 without being
        # allowed to act. It is allowed to act now, on the same-service subset
        # its own shadow record validated.
        #
        # Committed before the Slack round trip for the same reason the dedup
        # branch above commits: the dedup advisory lock must not be held
        # across a network call. Nothing has been written since it was taken.
        try:
            fold_target = await _find_fold_target(
                db, cluster, title, description, now=datetime.now(timezone.utc)
            )
        except Exception as fold_err:
            # Fail open, like the shadow check below: a webhook that 500s makes
            # Alertmanager retry the whole group, and an extra thread is a far
            # smaller loss than an alert that never arrives.
            logger.warning(f"Fold lookup failed (non-fatal): {fold_err}")
            fold_target = None
        if fold_target is not None:
            await db.commit()
            if await _fold_alert_into_incident(
                db, fold_target, alert, title, extract_service(title)
            ):
                logger.info(
                    "Folded '%s' into incident %s (same service)",
                    title,
                    fold_target.id,
                )
                incidents_folded += 1
                reconciliations.append({
                    "alertname": alert["alertname"],
                    "matched": True,
                    "reason": "folded_into_correlated_incident",
                    "incident_id": str(fold_target.id),
                })
                continue
            # The notice never reached Slack. Fall through and open the
            # incident — a duplicate thread beats an alert nobody is told
            # about. Re-take the dedup lock that the commit above released.
            await crud.lock_incident_dedup(db, cluster.id, title)

        # Create incident
        incident_data = schemas.IncidentCreate(
            title=title,
            description=description,
            severity=severity,
        )
        incident = await crud.create_incident(db, incident_data, cluster.id)
        incidents_created += 1
        logger.info(f"Created incident {incident.id} for alert '{alert['alertname']}' on cluster {cluster.id}")

        try:
            await _record_correlation_shadow(db, cluster, incident)
        except Exception as correlation_err:
            logger.warning(f"Correlation shadow check failed (non-fatal): {correlation_err}")

        # Create a durable investigation job (lease-backed; survives process loss).
        from sre_agent.job_worker import enqueue_and_kick

        job = await enqueue_and_kick(
            db=db,
            cluster_id=cluster.id,
            organization_id=cluster.org_id,
            incident_id=incident.id,
            alert_name=alert["alertname"],
            alert_labels=alert.get("labels") or {},
            alert_annotations={
                "summary": alert.get("summary", ""),
                "description": alert.get("description", ""),
            },
            alert_starts_at=alert.get("startsAt") or alert.get("starts_at"),
            alert_severity=alert.get("severity") or "warning",
            triggered_by="alertmanager_webhook",
        )
        logger.info(f"Queued durable job {job.id} for incident {incident.id}")

    return {
        "received": len(alerts),
        "incidents_created": incidents_created,
        "incidents_folded": incidents_folded,
        "resolved_reconciled": resolved_reconciled,
        "reconciliations": reconciliations,
    }
