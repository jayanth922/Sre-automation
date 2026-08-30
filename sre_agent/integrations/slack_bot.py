#!/usr/bin/env python3
"""
Slack transport for the SRE agent (project #3, the actual chat integration).

This registers the SRE agent as a Slack app member. Two ways to talk to it:
- @-mention it anywhere: routed through the ad hoc NL-query / chat dispatcher
  (`nl_query.handle_chat_message`), or through the tracked incident's real
  conversational context if the mention lands inside an open war-room thread.
- Reply directly inside a war-room thread (no @mention needed — the natural
  way to respond): captured by the `message` event and routed through the
  same memory-backed conversational endpoint the dashboard chat uses
  (`mission_control.handle_incident_message`, via `war_room.route_thread_reply`).

Design:
- `format_reply` and `process_mention` are pure/injectable and unit-tested (no
  Slack, no MCP required).
- `build_slack_app` lazily imports `slack_bolt` (a real dependency you add only
  when deploying the bot) and wires both events. If `slack_bolt` isn't
  installed the module still imports, so the logic stays testable.

Deploy:
    pip install "slack_bolt>=1.18"
    export SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-...   # socket mode
    python -m sre_agent.integrations.slack_bot
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

Handler = Callable[[str, Optional[str]], Awaitable[Dict[str, Any]]]


async def _default_handler(text: str, incident_id: Optional[str]) -> Dict[str, Any]:
    from ..nl_query import handle_chat_message
    return await handle_chat_message(text, incident_id)


def format_reply(result: Dict[str, Any]) -> str:
    """Turn a handle_chat_message result into a human Slack reply."""
    mode = result.get("mode")
    if mode == "greeting":
        return "👋 On-call SRE agent here — ask me for a metric or tell me how to steer the investigation."
    if mode == "query":
        if not result.get("valid"):
            return f"I couldn't turn that into a safe query: {result.get('error', 'unknown reason')}"
        if result.get("executed"):
            return f"`{result.get('promql')}`\n→ {result.get('data')}"
        return f"Generated `{result.get('promql')}` but couldn't execute it: {result.get('error', '')}".strip()
    if mode == "steer":
        return "Got it — I'll fold that into the live investigation at the next checkpoint."
    return "Sorry, I didn't understand that."


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[\w]+>", "", text or "").strip()


async def process_mention(
    text: str,
    incident_id: Optional[str],
    respond: Callable[[str], Awaitable[Any]],
    handler: Optional[Handler] = None,
) -> str:
    """Route a mention through the dispatcher and post the reply. Returns the reply."""
    handler = handler or _default_handler
    result = await handler(_strip_mention(text), incident_id)
    reply = format_reply(result)
    await respond(reply)
    return reply


def build_slack_app(registry=None):
    """Build the Slack Bolt app wired to the SRE agent (lazy import).

    `registry` is the shared `WarRoomRegistry` (see `war_room_service.py`)
    mapping Slack threads to incidents. Without it, only bare @mentions work
    (ad hoc, no incident context). With it, two things change: an @mention
    inside a tracked war-room thread resolves its `incident_id` instead of
    always answering cold, and a plain in-thread reply (no @mention — the
    natural way to respond) is captured and routed through the real,
    memory-backed conversational endpoint the dashboard chat already uses.
    """
    try:
        from slack_bolt.async_app import AsyncApp  # lazy; optional dependency
    except Exception as e:  # pragma: no cover - only without slack_bolt
        raise RuntimeError("slack_bolt not installed; run: pip install 'slack_bolt>=1.18'") from e

    from ..war_room import ThreadRef, route_thread_reply

    app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"))

    @app.event("app_mention")
    async def _on_mention(event, say):  # pragma: no cover - requires Slack
        thread = ThreadRef(
            channel=event.get("channel", ""),
            thread_ts=event.get("thread_ts") or event.get("ts", ""),
        )
        incident_id = registry.incident_for(thread) if registry else None
        await process_mention(
            event.get("text", ""), incident_id,
            respond=lambda msg: say(text=msg, thread_ts=event.get("ts")),
        )

    if registry is not None:
        @app.event("message")
        async def _on_thread_message(event, say):  # pragma: no cover - requires Slack
            # Only react to human replies inside a tracked war-room thread.
            if event.get("bot_id") or not event.get("thread_ts"):
                return
            thread = ThreadRef(channel=event.get("channel", ""), thread_ts=event.get("thread_ts", ""))
            if not registry.is_war_room(thread):
                return

            async def poster(_thread, text):
                await say(text=text, thread_ts=thread.thread_ts)

            await route_thread_reply(event.get("text", ""), thread, registry, poster)

    return app


async def _run_async() -> None:  # pragma: no cover - requires Slack tokens
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from ..war_room_service import _get_registry

    registry = await _get_registry()
    app = build_slack_app(registry)
    handler = AsyncSocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    await handler.start_async()


def run() -> None:  # pragma: no cover - requires Slack tokens
    import asyncio

    asyncio.run(_run_async())


if __name__ == "__main__":  # pragma: no cover
    run()
