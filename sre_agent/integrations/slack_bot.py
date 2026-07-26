#!/usr/bin/env python3
"""
Slack transport for the SRE agent (project #3, the actual chat integration).

This is the "tag the agent in Slack and it responds" layer that was previously
only documented. It registers the SRE agent as a Slack app member: when it's
@-mentioned, the message is routed through the tested NL-query / chat dispatcher
(`nl_query.handle_chat_message`) and the reply is posted back to the thread.

Design:
- `format_reply` and `process_mention` are pure/injectable and unit-tested (no
  Slack, no MCP required).
- `build_slack_app` lazily imports `slack_bolt` (a real dependency you add only
  when deploying the bot) and wires the app-mention event. If `slack_bolt` isn't
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


def build_slack_app():
    """Build the Slack Bolt app wired to the SRE agent (lazy import)."""
    try:
        from slack_bolt.async_app import AsyncApp  # lazy; optional dependency
    except Exception as e:  # pragma: no cover - only without slack_bolt
        raise RuntimeError("slack_bolt not installed; run: pip install 'slack_bolt>=1.18'") from e

    app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"))

    @app.event("app_mention")
    async def _on_mention(event, say):  # pragma: no cover - requires Slack
        incident_id = None  # a channel↔incident mapping can be injected here
        await process_mention(
            event.get("text", ""), incident_id,
            respond=lambda msg: say(text=msg, thread_ts=event.get("ts")),
        )

    return app


def run() -> None:  # pragma: no cover - requires Slack tokens
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    app = build_slack_app()
    handler = AsyncSocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    import asyncio

    asyncio.run(handler.start_async())


if __name__ == "__main__":  # pragma: no cover
    run()
