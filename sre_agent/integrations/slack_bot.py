#!/usr/bin/env python3
"""
Slack transport for the SRE agent (project #3, the actual chat integration).

This registers the SRE agent as a Slack app member. Two ways to talk to it:
- @-mention it outside a war room: routed through the ad hoc NL-query / chat
  dispatcher (`nl_query.handle_chat_message`), which answers metric questions
  cold and has no incident to act on.
- Say anything inside a war-room thread, @mention or not: routed through the
  incident's real, memory-backed conversational turn
  (`mission_control.handle_incident_message`, via `war_room.route_thread_reply`),
  after the approval commands get first refusal on the text.

Slack delivers a mention inside a channel the bot is in as *both* an
`app_mention` and a `message`, so the two handlers below funnel war-room text
through one shared body guarded by `_claim_event`: whichever event arrives
first handles the message, the other is dropped.

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
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Dict[str, Any]]]


async def _default_handler(
    text: str, incident_id: Optional[str], session_key: Optional[str] = None
) -> Dict[str, Any]:
    from ..nl_query import handle_chat_message
    return await handle_chat_message(text, incident_id, session_key=session_key)


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
        # Reached only by a caller that hands the ad hoc dispatcher an incident
        # id. That dispatcher cannot touch a running investigation — only
        # `war_room.route_thread_reply` can — and the Slack transport sends
        # war-room text straight there, so this branch no longer fires from
        # Slack at all. Say what is true instead of confirming a fold that
        # never happens.
        return (
            "I can't change a running investigation from here — post that as a reply "
            "in the incident's Slack thread and I'll pick it up there."
        )
    if mode == "chat":
        return result.get("reply") or "Sorry, I didn't understand that."
    return "Sorry, I didn't understand that."


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[\w]+>", "", text or "").strip()


_CLAIM_CAPACITY = 512
_claimed_events: "OrderedDict[str, None]" = OrderedDict()


def _claim_event(channel: str, ts: str) -> bool:
    """Claim one Slack message so exactly one handler acts on it.

    A message that @-mentions the bot inside a channel the bot belongs to
    arrives twice — once as `app_mention`, once as `message` — carrying the
    same `(channel, ts)`. Both now route war-room text into the incident's
    real conversational turn, so without a claim one sentence buys two agent
    turns and two replies. Whichever event Slack delivers first wins; the
    second is dropped. That makes the outcome independent of the delivery
    order, and of whether a given workspace subscribes to both events.

    Bounded, because socket mode runs for weeks: the oldest claims are evicted
    once the table is full, which is safe because a duplicate lands within
    milliseconds of its original. A message with no `ts` is never deduplicated
    — better a rare double reply than a silently dropped question.
    """
    if not ts:
        return True
    key = f"{channel}:{ts}"
    if key in _claimed_events:
        return False
    _claimed_events[key] = None
    while len(_claimed_events) > _CLAIM_CAPACITY:
        _claimed_events.popitem(last=False)
    return True


async def process_mention(
    text: str,
    incident_id: Optional[str],
    respond: Callable[[str], Awaitable[Any]],
    handler: Optional[Handler] = None,
    session_key: Optional[str] = None,
) -> str:
    """Route a mention through the dispatcher and post the reply. Returns the reply.

    `session_key` (e.g. a stable channel+user id) enables short-term memory for
    the ad hoc 'chat' path (see nl_query._handle_ad_hoc_chat); omitted entirely
    from the handler call when not provided, so injected test handlers with the
    original 2-arg (text, incident_id) signature keep working unchanged.
    """
    handler = handler or _default_handler
    stripped = _strip_mention(text)
    if session_key is not None:
        result = await handler(stripped, incident_id, session_key=session_key)
    else:
        result = await handler(stripped, incident_id)
    reply = format_reply(result)
    await respond(reply)
    return reply


async def _slack_user_email(app: Any, slack_user_id: Optional[str]) -> Optional[str]:
    """Resolve a Slack user id to the email on their Slack profile, so
    route_gate_command can authorize a gate decision against our own
    User table (there is no other identity bridge between Slack and the
    app). Requires the users:read.email OAuth scope; returns None (never
    raises) if the lookup fails or the scope is missing, which
    route_gate_command treats as "can't decide this gate here".
    """
    if not slack_user_id:
        return None
    try:
        resp = await app.client.users_info(user=slack_user_id)
        return (resp.get("user") or {}).get("profile", {}).get("email")
    except Exception:
        logger.warning("slack_user_email_lookup_failed", extra={"slack_user_id": slack_user_id})
        return None


def build_slack_app(registry=None, organization: Any = None):
    """Build the Slack Bolt app wired to the SRE agent (lazy import).

    `registry` is the shared `WarRoomRegistry` (see `war_room_service.py`)
    mapping Slack threads to incidents. Without it, only bare @mentions work
    (ad hoc, no incident context). With it, anything said inside a tracked
    war-room thread — @mention or plain reply — reaches the incident's real,
    memory-backed conversational turn, through one shared body so the two
    entry points cannot answer the same question differently.

    `organization` (Phase 4) is the owning `Organization` row: when it has a
    manually-pasted, verified bot token (`slack_oauth.resolve_slack_bot_token`),
    that token is used instead of the static `SLACK_BOT_TOKEN` env var. Bolt's
    socket-mode `AsyncApp` is bound to one token per process either way, so
    this only changes *which* token a given process's bot uses — not whether
    one process can serve many workspaces.
    """
    try:
        from slack_bolt.async_app import AsyncApp  # lazy; optional dependency
    except Exception as e:  # pragma: no cover - only without slack_bolt
        raise RuntimeError("slack_bolt not installed; run: pip install 'slack_bolt>=1.18'") from e

    from ..war_room import (
        ThreadRef,
        is_ack_command,
        is_approval_intent_near_miss,
        is_fix_approval_command,
        is_resolve_command,
        parse_gate_command,
        route_ack_command,
        route_fix_approval_command,
        route_gate_command,
        route_resolve_command,
        route_thread_reply,
    )
    from ..multitenant.slack_oauth import resolve_slack_bot_token

    token = resolve_slack_bot_token(organization) if organization is not None else os.getenv("SLACK_BOT_TOKEN")
    app = AsyncApp(token=token)

    async def _route_war_room_text(text, thread, event, say):
        """Handle one human message inside a tracked war-room thread.

        Shared by both events deliberately. Saying something to the bot in an
        incident thread is one act whether or not you @-mention it, and while
        the two had separate bodies they answered it differently: the mention
        went to the ad hoc dispatcher, which cannot steer an investigation and
        claimed it would anyway.

        The bot-echo guard and `_claim_event` sit here rather than in either
        caller because this is the only place both can reach. The echo guard
        matters more than it used to: the agent's own war-room posts can carry
        an @mention, and a mention in a war room now starts a real
        investigation turn rather than an inert sentence. The claim must run
        *after* the war-room filters, so ordinary channel chatter never evicts
        a real claim.
        """
        if event.get("bot_id"):
            return
        if not _claim_event(event.get("channel", ""), event.get("ts", "")):
            return

        async def poster(_thread, message):
            await say(text=message, thread_ts=thread.thread_ts)

        if is_ack_command(text):
            approver_email = await _slack_user_email(app, event.get("user"))
            await route_ack_command(text, thread, registry, approver_email, poster)
            return
        if is_resolve_command(text):
            approver_email = await _slack_user_email(app, event.get("user"))
            await route_resolve_command(
                text, thread, registry, approver_email, poster
            )
            return
        if parse_gate_command(text) is not None:
            approver_email = await _slack_user_email(app, event.get("user"))
            await route_gate_command(text, thread, registry, approver_email, poster)
            return
        if is_fix_approval_command(text):
            approver_email = await _slack_user_email(app, event.get("user"))
            await route_fix_approval_command(text, thread, registry, approver_email, poster)
            return
        if is_approval_intent_near_miss(text):
            # Deliberately does NOT reach the LLM chat path here: that
            # path has no structural signal for "did an approval actually
            # happen" and will narrate a plausible-sounding but false
            # confirmation instead of surfacing that nothing was recorded.
            await poster(
                thread,
                "To actually authorize the pending remediation I need the exact "
                "phrase *approve fix* sent as its own message — could you resend it?",
            )
            return

        # Resolve the asker the same way the approval commands resolve the
        # approver: a follow-up question is the third turn of the incident's
        # Langfuse session, and without this it lands with userId null —
        # over Slack, the only channel, that loses who asked.
        asker_email = await _slack_user_email(app, event.get("user"))
        await route_thread_reply(
            text, thread, registry, poster, asker_email=asker_email
        )

    @app.event("app_mention")
    async def _on_mention(event, say):  # pragma: no cover - requires Slack
        thread = ThreadRef(
            channel=event.get("channel", ""),
            thread_ts=event.get("thread_ts") or event.get("ts", ""),
        )
        incident_id = registry.incident_for(thread) if registry else None
        if incident_id is not None:
            # Inside a war room the mention is just how this operator chose to
            # address the bot. The mention token itself is transport noise, so
            # it is stripped before the text reaches the incident's turn; the
            # command matchers normalize it away on their own either way.
            await _route_war_room_text(
                _strip_mention(event.get("text", "")), thread, event, say
            )
            return
        session_key = f"slack-chat:{event.get('channel', '')}:{event.get('user', 'anon')}"
        await process_mention(
            event.get("text", ""), incident_id,
            respond=lambda msg: say(text=msg, thread_ts=event.get("ts")),
            session_key=session_key,
        )

    if registry is not None:
        @app.event("message")
        async def _on_thread_message(event, say):  # pragma: no cover - requires Slack
            # Only react to replies that are actually inside a thread; the
            # shared body below drops the bot's own messages.
            if not event.get("thread_ts"):
                return
            thread = ThreadRef(channel=event.get("channel", ""), thread_ts=event.get("thread_ts", ""))
            if not registry.is_war_room(thread):
                return
            await _route_war_room_text(event.get("text", ""), thread, event, say)

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
