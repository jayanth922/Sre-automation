"""War-room forwarder — mirrors an incident's investigation into Slack.

When the incident's owning cluster's organization has a Slack bot token
configured, opening a war room posts an "incident opened" message and then
streams the agent's surfaced timeline events into that Slack thread via
war_room.forward_events. Entirely optional: without an org Slack token this
is a clean no-op, and every failure is non-fatal to incident processing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level singleton: the two Bolt event handlers (app_mention, message)
# and maybe_open_war_room all run inside this same FastAPI process, so an
# in-process registry needs no cross-process sync. It is restart-safe because
# it's rehydrated from the DB (Incident.slack_channel/slack_thread_ts) on
# first use rather than trusted to survive on its own.
_registry: Optional["WarRoomRegistry"] = None  # noqa: F821 - forward ref, imported lazily
_registry_hydrated = False
_registry_lock = asyncio.Lock()


async def _get_registry():
    """Return the shared WarRoomRegistry, rehydrating it from open Slack
    threads in the DB the first time it's needed (works whether or not the
    FastAPI startup hook has run yet)."""
    global _registry, _registry_hydrated
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    if _registry is None:
        _registry = WarRoomRegistry()

    if not _registry_hydrated:
        async with _registry_lock:
            if not _registry_hydrated:
                try:
                    from backend import crud, database

                    async with database.AsyncSessionLocal() as db:
                        incidents = await crud.get_incidents_with_open_slack_threads(db)
                    for incident in incidents:
                        _registry.open(
                            str(incident.id),
                            ThreadRef(incident.slack_channel, incident.slack_thread_ts),
                        )
                    logger.info(
                        f"war-room: rehydrated {len(incidents)} open Slack thread(s)"
                    )
                except Exception as e:
                    logger.warning(f"war-room: registry rehydration failed (non-fatal): {e}")
                _registry_hydrated = True

    return _registry


async def close_war_room(incident_id: str) -> None:
    """Best-effort: drop a resolved incident's war-room mapping from the
    registry so late Slack replies in that thread stop being routed."""
    try:
        registry = await _get_registry()
        registry.close(incident_id)
    except Exception as e:
        logger.debug(f"war-room: close skipped (non-fatal): {e}")


async def post_to_incident_thread(incident_id: str, text: str) -> bool:
    """Post one message into an incident's existing Slack thread.

    The war room's normal outbound path is `forward_events`, an asyncio task
    that `maybe_open_war_room` starts when the thread is opened. That task
    does not survive a process restart — the registry is rehydrated from the
    DB, the forwarder is not — so anything that must reach the on-call
    *after* a restart cannot go through the bus. This posts straight to the
    channel/ts persisted on the incident row, with the owning org's own
    token, and reports whether it actually reached Slack.

    Returns False (never raises) when the org has no Slack token, the
    incident has no thread, or Slack rejects the post.
    """
    from backend import crud, database, models

    try:
        async with database.AsyncSessionLocal() as db:
            incident = await db.get(models.Incident, uuid.UUID(str(incident_id)))
            if incident is None or not incident.slack_channel or not incident.slack_thread_ts:
                return False
            channel = incident.slack_channel
            thread_ts = incident.slack_thread_ts
            cluster = await crud.get_cluster_by_id(db, incident.cluster_id)
            org = await crud.get_org_by_id(db, cluster.org_id) if cluster else None
        if org is None:
            return False
        from sre_agent.multitenant.slack_oauth import resolve_slack_bot_token

        token = resolve_slack_bot_token(org)
        if not token:
            return False
        from slack_bolt.async_app import AsyncApp

        app = AsyncApp(token=token)
        await app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        return True
    except Exception as exc:
        logger.warning("war-room: thread post failed for %s (non-fatal): %s", incident_id, exc)
        return False


def investigation_failed_text(error: str) -> str:
    """The message a dead investigation leaves in its Slack thread.

    Silence is the wrong default here. A thread that says "Incident opened"
    and then stops reads, to the person watching it, exactly like an
    investigation still in progress — so the notice has to say all three
    things: that it failed, that nothing was diagnosed or remediated, and
    what went wrong. The error is truncated because provider exceptions
    arrive with whole JSON bodies in them and Slack is not a log viewer.
    """
    detail = error if len(error) <= 500 else error[:500] + "…"
    return (
        ":x: *Investigation failed — no findings were produced.*\n"
        "This incident is still open and nothing has been diagnosed or "
        "remediated; it needs a human.\n"
        f"```{detail}```"
    )


def _opening_text(summary: str) -> str:
    """Compose the Slack open message, mentioning on-call when configured."""
    mention = ""
    try:
        from sre_agent.oncall import format_slack_mention, resolve_oncall

        handle = resolve_oncall()
        if handle:
            mention = f"\nOn-call: {format_slack_mention(handle)}"
    except Exception as exc:  # pragma: no cover - never block war-room open
        logger.debug("war-room: on-call resolve skipped (%s)", exc)
    return f":rotating_light: *Incident opened*\n{summary}{mention}"


async def maybe_open_war_room(incident_id: str, cluster_id: str, summary: str) -> None:
    """Open a Slack war-room thread for this incident and stream its events.

    No-op unless the owning cluster's organization has a Slack bot token
    configured (OAuth-installed or pasted manually in Settings) — there is no
    process-wide SLACK_BOT_TOKEN fallback here: each org's war-room messages
    must post with that org's own token, never another org's or the operator's."""
    token: Optional[str] = None
    try:
        from backend import crud, database

        async with database.AsyncSessionLocal() as db:
            cluster = await crud.get_cluster_by_id(db, uuid.UUID(str(cluster_id)))
            if cluster is not None:
                org = await crud.get_org_by_id(db, cluster.org_id)
                if org is not None:
                    from sre_agent.multitenant.slack_oauth import resolve_slack_bot_token

                    token = resolve_slack_bot_token(org)
    except Exception as e:
        logger.debug(f"war-room: cluster/org Slack token lookup skipped ({e})")
    if not token:
        return
    try:
        from slack_bolt.async_app import AsyncApp
    except Exception:
        logger.info("war-room: slack_bolt not installed; skipping Slack forwarding")
        return

    try:
        from sre_agent.war_room import ThreadRef, forward_events

        app = AsyncApp(token=token)
        channel = os.getenv("SLACK_WAR_ROOM_CHANNEL", "#incidents")
        opened = await app.client.chat_postMessage(
            channel=channel,
            text=_opening_text(summary),
        )
        thread_ts = opened.get("ts")
        resolved_channel = opened.get("channel", channel)

        registry = await _get_registry()
        registry.open(incident_id, ThreadRef(resolved_channel, thread_ts))

        try:
            from backend import crud, database

            async with database.AsyncSessionLocal() as db:
                await crud.set_incident_slack_thread(
                    db, uuid.UUID(incident_id), resolved_channel, thread_ts
                )
        except Exception as e:
            logger.warning(f"war-room: could not persist Slack thread mapping (non-fatal): {e}")

        async def poster(_thread, text: str) -> None:
            await app.client.chat_postMessage(
                channel=resolved_channel, thread_ts=thread_ts, text=text
            )

        # Stream this incident's surfaced events into the thread (long-running).
        asyncio.create_task(forward_events(incident_id, poster, registry=registry))
        logger.info(f"war-room: opened Slack thread for incident {incident_id}")
    except Exception as e:
        logger.warning(f"war-room: could not open Slack thread (non-fatal): {e}")


async def run_slack_bot() -> None:
    """Start the Slack bot (socket mode) so on-call engineers can reply in a
    war-room thread — "approve fix", "approve/deny {gate}", "ack", or a plain
    question routed to the memory-backed investigation handler. This is the
    only inbound channel in the Slack-only approval/communication design, so
    it must start whenever a bot is actually installed.

    Requires SLACK_APP_TOKEN (the Slack app's Socket Mode app-level token,
    xapp-..., generated once in the Slack app's settings — distinct from any
    org's installed bot token). The bot token itself comes from whichever
    organization has one installed (OAuth or manually pasted in Settings), not
    from a global env var: a single process serves one Slack app connection,
    so with multiple orgs the first one with a token installed is used."""
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not app_token:
        return
    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from sre_agent.integrations.slack_bot import build_slack_app
    except Exception as e:
        logger.info(f"slack bot: not started ({e})")
        return
    try:
        organization = None
        if not os.getenv("SLACK_BOT_TOKEN"):
            from backend import crud, database

            async with database.AsyncSessionLocal() as db:
                organization = await crud.get_org_with_slack_bot_token(db)
            if organization is None:
                logger.info("slack bot: not started (no organization has a Slack bot token installed)")
                return

        registry = await _get_registry()
        app = build_slack_app(registry, organization=organization)
        handler = AsyncSocketModeHandler(app, app_token)
        logger.info("🤖 Slack bot connecting (socket mode)…")
        await handler.start_async()  # long-running
    except Exception as e:
        logger.warning(f"slack bot failed to start (non-fatal): {e}")
