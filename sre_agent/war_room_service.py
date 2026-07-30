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

logger = logging.getLogger(__name__)


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
        from sre_agent.war_room import forward_events

        app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"))
        channel = os.getenv("SLACK_WAR_ROOM_CHANNEL", "#incidents")
        opened = await app.client.chat_postMessage(
            channel=channel,
            text=f":rotating_light: *Incident opened*\n{summary}",
        )
        thread_ts = opened.get("ts")
        resolved_channel = opened.get("channel", channel)

        async def poster(_thread, text: str) -> None:
            await app.client.chat_postMessage(
                channel=resolved_channel, thread_ts=thread_ts, text=text
            )

        # Stream this incident's surfaced events into the thread (long-running).
        asyncio.create_task(forward_events(incident_id, poster))
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
        app = build_slack_app()
        handler = AsyncSocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
        logger.info("🤖 Slack bot connecting (socket mode)…")
        await handler.start_async()  # long-running
    except Exception as e:
        logger.warning(f"slack bot failed to start (non-fatal): {e}")
