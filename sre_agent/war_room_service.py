"""War-room forwarder — mirrors an incident's investigation into Slack.

When Slack is configured (SLACK_BOT_TOKEN set), opening a war room posts an
"incident opened" message and then streams the agent's surfaced timeline events
into that Slack thread via war_room.forward_events. Entirely optional: without
Slack tokens this is a clean no-op, and every failure is non-fatal to incident
processing.
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


async def maybe_open_war_room(incident_id: str, summary: str) -> None:
    """Open a Slack war-room thread for this incident and stream its events.
    No-op unless SLACK_BOT_TOKEN is configured."""
    if not os.getenv("SLACK_BOT_TOKEN"):
        return
    try:
        from slack_bolt.async_app import AsyncApp
    except Exception:
        logger.info("war-room: slack_bolt not installed; skipping Slack forwarding")
        return

    try:
        from sre_agent.war_room import ThreadRef, forward_events

        app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"))
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
    """Start the Slack bot (socket mode) so on-call engineers can @mention it in
    their channel — it answers verified metric questions and steers the active
    investigation. No-op unless SLACK_BOT_TOKEN and SLACK_APP_TOKEN are set."""
    if not (os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APP_TOKEN")):
        return
    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from sre_agent.integrations.slack_bot import build_slack_app
    except Exception as e:
        logger.info(f"slack bot: not started ({e})")
        return
    try:
        registry = await _get_registry()
        app = build_slack_app(registry)
        handler = AsyncSocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
        logger.info("🤖 Slack bot connecting (socket mode)…")
        await handler.start_async()  # long-running
    except Exception as e:
        logger.warning(f"slack bot failed to start (non-fatal): {e}")
