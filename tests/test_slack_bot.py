#!/usr/bin/env python3
"""Unit tests for the Slack transport (project #3). No Slack/MCP needed."""

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Load the module by path (its lazy slack_bolt import means it loads fine without it).
_PKG = Path(__file__).resolve().parents[1] / "sre_agent" / "integrations"
_spec = importlib.util.spec_from_file_location("slack_bot", _PKG / "slack_bot.py")
sb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sb
_spec.loader.exec_module(sb)


def _install_fake_slack_bolt():
    """Inject a fake slack_bolt so build_slack_app's real relative imports
    (from ..war_room import ...) resolve — requires importing the module as
    part of its real package, unlike the by-path `sb` above."""

    class FakeAsyncApp:
        def __init__(self, token=None):
            self.token = token
            self.handlers = {}

        def event(self, name):
            def decorator(fn):
                self.handlers[name] = fn
                return fn

            return decorator

    fake_pkg = types.ModuleType("slack_bolt")
    fake_async_app_mod = types.ModuleType("slack_bolt.async_app")
    fake_async_app_mod.AsyncApp = FakeAsyncApp
    fake_pkg.async_app = fake_async_app_mod
    sys.modules["slack_bolt"] = fake_pkg
    sys.modules["slack_bolt.async_app"] = fake_async_app_mod


def _build_real_app_with_registry(registry):
    _install_fake_slack_bolt()
    from sre_agent.integrations import slack_bot as real_sb

    importlib.reload(real_sb)
    return real_sb.build_slack_app(registry)


def test_format_reply_modes():
    assert "SRE agent" in sb.format_reply({"mode": "greeting"})
    steer = sb.format_reply({"mode": "steer"})
    assert "fold that into" not in steer          # it never did fold it in
    assert "incident's Slack thread" in steer
    q = sb.format_reply({"mode": "query", "valid": True, "executed": True, "promql": "sum(x)", "data": [1]})
    assert "sum(x)" in q and "[1]" in q


def test_format_reply_invalid_query():
    r = sb.format_reply({"mode": "query", "valid": False, "error": "bad metric"})
    assert "couldn't turn that into a safe query" in r
    assert "bad metric" in r


def test_format_reply_chat_mode_uses_llm_reply_verbatim():
    r = sb.format_reply({"mode": "chat", "reply": "I'm not sure — can you clarify?", "llm_used": True})
    assert r == "I'm not sure — can you clarify?"


def test_process_mention_without_session_key_calls_two_arg_handler():
    """Backward-compat: omitting session_key must not break handlers with the
    original (text, incident_id) signature."""
    posted = {}

    async def respond(msg):
        posted["msg"] = msg

    async def fake_handler(text, incident_id):
        return {"mode": "chat", "reply": "hey"}

    reply = asyncio.run(sb.process_mention("<@U1> hi", None, respond, handler=fake_handler))
    assert reply == "hey"


def test_process_mention_with_session_key_passes_it_through():
    seen = {}

    async def respond(_):
        pass

    async def fake_handler(text, incident_id, session_key=None):
        seen["session_key"] = session_key
        return {"mode": "chat", "reply": "hey"}

    asyncio.run(
        sb.process_mention("<@U1> hi", None, respond, handler=fake_handler, session_key="slack-chat:C1:U1")
    )
    assert seen["session_key"] == "slack-chat:C1:U1"


def test_process_mention_strips_mention_and_replies():
    posted = {}

    async def respond(msg):
        posted["msg"] = msg

    async def fake_handler(text, incident_id):
        posted["seen_text"] = text
        return {"mode": "steer"}

    reply = asyncio.run(sb.process_mention("<@U123> focus on logs", "inc-1", respond, handler=fake_handler))
    assert posted["seen_text"] == "focus on logs"          # mention stripped
    assert posted["msg"] == reply                          # reply posted
    assert "incident's Slack thread" in reply              # and it points somewhere real


def test_process_mention_query_path():
    async def respond(_):
        pass

    async def fake_handler(text, incident_id):
        return {"mode": "query", "valid": True, "executed": True, "promql": "rate(errors[5m])", "data": 0.03}

    reply = asyncio.run(sb.process_mention("<@U1> checkout error rate", None, respond, handler=fake_handler))
    assert "rate(errors[5m])" in reply


def test_a_mention_inside_a_war_room_reaches_the_real_conversational_turn():
    """The module docstring promised this long before the code did it.

    `_on_mention` used to resolve the incident id from the registry and then
    hand it to the ad hoc dispatcher, which cannot touch a running
    investigation. The operator got "I'll fold that into the live
    investigation at the next checkpoint" and nothing folded it in. A mention
    inside a war room is the same act as a reply inside it, so it takes the
    same path.
    """
    import sre_agent.war_room as war_room_mod
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)

    posted = []

    async def fake_say(text, thread_ts):
        posted.append((text, thread_ts))

    seen = {}

    async def fake_default_handler(text, incident_id, asker_email=None):
        seen["text"] = text
        seen["incident_id"] = incident_id
        return {"status": "RESPONDED", "response": "Rolled back at 14:02."}

    original = war_room_mod._default_handler
    war_room_mod._default_handler = fake_default_handler
    try:
        asyncio.run(
            app.handlers["app_mention"](
                {
                    "channel": "C1",
                    "thread_ts": "T1",
                    "ts": "T5",
                    "text": "<@U9> what changed right before this?",
                    "user": "U42",
                },
                fake_say,
            )
        )
    finally:
        war_room_mod._default_handler = original

    assert seen["incident_id"] == "inc-1"
    assert seen["text"] == "what changed right before this?"   # mention stripped
    # Posted into the war-room thread, not as a reply to the mention itself.
    assert posted == [("Rolled back at 14:02.", "T1")]


def test_one_mention_buys_one_agent_turn_not_two():
    """Slack delivers a mention in a channel the bot is in as both an
    `app_mention` and a `message`, carrying the same `(channel, ts)`. Both
    handlers now route war-room text into a real agent turn, so without the
    claim one sentence costs two investigations and posts two answers.

    Whichever event arrives first must win, because which one that is isn't
    ours to decide — so this drives both orders.
    """
    import sre_agent.war_room as war_room_mod
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    def run(first, second):
        registry = WarRoomRegistry()
        registry.open("inc-1", ThreadRef("C1", "T1"))
        app = _build_real_app_with_registry(registry)

        posted = []
        turns = []

        async def fake_say(text, thread_ts):
            posted.append(text)

        async def fake_default_handler(text, incident_id, asker_email=None):
            turns.append(text)
            return {"status": "RESPONDED", "response": "once"}

        event = {
            "channel": "C1",
            "thread_ts": "T1",
            "ts": "T5",
            "text": "<@U9> is the error rate back down?",
            "user": "U42",
        }

        original = war_room_mod._default_handler
        war_room_mod._default_handler = fake_default_handler
        try:

            async def both():
                await app.handlers[first](dict(event), fake_say)
                await app.handlers[second](dict(event), fake_say)

            asyncio.run(both())
        finally:
            war_room_mod._default_handler = original
        return turns, posted

    turns, posted = run("app_mention", "message")
    assert len(turns) == 1 and posted == ["once"]

    turns, posted = run("message", "app_mention")
    assert len(turns) == 1 and posted == ["once"]


def test_the_bot_never_answers_its_own_mention():
    """The agent's own war-room posts can carry an @mention.

    While a mention only produced an inert sentence that cost nothing; now it
    starts a real investigation turn, so an echo of the bot's own message is a
    loop. Both events drop it, because the guard lives in the body they share
    rather than in one of them.
    """
    import sre_agent.war_room as war_room_mod
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)

    posted = []

    async def fake_say(text, thread_ts):
        posted.append(text)

    async def fake_default_handler(text, incident_id, asker_email=None):  # pragma: no cover
        raise AssertionError("the bot must not investigate its own message")

    event = {
        "bot_id": "B1",
        "channel": "C1",
        "thread_ts": "T1",
        "ts": "T8",
        "text": "<@U9> paging the on-call",
        "user": "U9",
    }

    original = war_room_mod._default_handler
    war_room_mod._default_handler = fake_default_handler
    try:

        async def both():
            await app.handlers["app_mention"](dict(event), fake_say)
            await app.handlers["message"](dict(event), fake_say)

        asyncio.run(both())
    finally:
        war_room_mod._default_handler = original

    assert posted == []


def test_a_mention_outside_a_war_room_still_answers_cold():
    """Closing the war-room path must not make the bot mute everywhere else.
    A mention with no tracked incident behind it keeps going to the ad hoc
    dispatcher, and keeps replying under the mention.
    """
    from sre_agent.integrations import slack_bot as real_sb
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)

    posted = []

    async def fake_say(text, thread_ts):
        posted.append((text, thread_ts))

    seen = {}

    async def fake_dispatcher(text, incident_id, session_key=None):
        seen["incident_id"] = incident_id
        seen["session_key"] = session_key
        return {"mode": "chat", "reply": "No incident open on that cluster."}

    original = real_sb._default_handler
    real_sb._default_handler = fake_dispatcher
    try:
        asyncio.run(
            app.handlers["app_mention"](
                {"channel": "C9", "ts": "T9", "text": "<@U9> anything on fire?", "user": "U42"},
                fake_say,
            )
        )
    finally:
        real_sb._default_handler = original

    assert seen["incident_id"] is None
    assert seen["session_key"] == "slack-chat:C9:U42"
    assert posted == [("No incident open on that cluster.", "T9")]


def test_approval_commands_are_reachable_by_mention_too():
    """`@sre approve fix` is a command, not a question.

    The matchers already normalize a mention prefix away, but before the two
    handlers shared a body only a plain reply ever reached them: an @-mentioned
    approval went to the chat dispatcher instead. On the product's only
    communication surface, an approval that silently becomes small talk is the
    worst possible failure.
    """
    import sre_agent.war_room as war_room_mod
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))

    calls = []

    async def fake_route_fix_approval(text, thread, reg, approver_email, poster):
        calls.append((text, thread.thread_ts))
        await poster(thread, "Approved — running the fix.")
        return {"status": "APPROVED"}

    async def fake_default_handler(text, incident_id, asker_email=None):  # pragma: no cover
        raise AssertionError("an approval must never fall through to the chat path")

    original_route = war_room_mod.route_fix_approval_command
    original_handler = war_room_mod._default_handler
    war_room_mod.route_fix_approval_command = fake_route_fix_approval
    war_room_mod._default_handler = fake_default_handler
    try:
        # build_slack_app imports the routes when it is called, so the patch
        # has to be in place before the app is built.
        app = _build_real_app_with_registry(registry)

        posted = []

        async def fake_say(text, thread_ts):
            posted.append((text, thread_ts))

        asyncio.run(
            app.handlers["app_mention"](
                {
                    "channel": "C1",
                    "thread_ts": "T1",
                    "ts": "T7",
                    "text": "<@U9> approve fix",
                    "user": "U42",
                },
                fake_say,
            )
        )
    finally:
        war_room_mod.route_fix_approval_command = original_route
        war_room_mod._default_handler = original_handler

    assert calls == [("approve fix", "T1")]
    assert posted == [("Approved — running the fix.", "T1")]


def test_the_claim_table_cannot_grow_without_bound():
    """Socket mode runs for weeks. Evicting the oldest claim is safe because a
    duplicate arrives within milliseconds of its original — but the eviction
    has to actually happen.
    """
    sb._claimed_events.clear()
    try:
        for i in range(sb._CLAIM_CAPACITY + 50):
            assert sb._claim_event("C1", f"ts-{i}") is True
        assert len(sb._claimed_events) <= sb._CLAIM_CAPACITY

        # The newest claim is still held...
        last = f"ts-{sb._CLAIM_CAPACITY + 49}"
        assert sb._claim_event("C1", last) is False
        # ...and the oldest has been evicted, so it would be handled again.
        assert sb._claim_event("C1", "ts-0") is True

        # A message with no ts is never deduplicated: a rare double reply beats
        # a question silently dropped.
        assert sb._claim_event("C1", "") is True
        assert sb._claim_event("C1", "") is True
    finally:
        sb._claimed_events.clear()


def test_merged_app_registers_message_handler_only_with_registry():
    app_without_registry = _build_real_app_with_registry(None)
    assert "message" not in app_without_registry.handlers
    assert "app_mention" in app_without_registry.handlers


def test_message_handler_ignores_bot_messages_and_non_war_room_threads():
    from sre_agent.war_room import ThreadRef, WarRoomRegistry

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)
    handler = app.handlers["message"]

    posted = []

    async def fake_say(text, thread_ts):
        posted.append((text, thread_ts))

    async def scenario():
        # Bot's own message: ignored even inside the tracked thread.
        await handler({"bot_id": "B1", "channel": "C1", "thread_ts": "T1", "text": "hi"}, fake_say)
        # No thread_ts at all (a top-level channel message): ignored.
        await handler({"channel": "C1", "text": "hi"}, fake_say)
        # Thread not tracked as a war room: ignored.
        await handler({"channel": "C9", "thread_ts": "T9", "text": "hi"}, fake_say)

    asyncio.run(scenario())
    assert posted == []


def test_message_handler_routes_tracked_war_room_reply():
    from sre_agent.war_room import ThreadRef, WarRoomRegistry
    import sre_agent.war_room as war_room_mod

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)
    handler = app.handlers["message"]

    posted = []

    async def fake_say(text, thread_ts):
        posted.append((text, thread_ts))

    async def fake_default_handler(text, incident_id, asker_email=None):
        assert incident_id == "inc-1"
        assert text == "what's the error rate?"
        return {"status": "RESPONDED", "response": "3%."}

    original_handler = war_room_mod._default_handler
    war_room_mod._default_handler = fake_default_handler
    try:
        asyncio.run(
            handler({"channel": "C1", "thread_ts": "T1", "text": "what's the error rate?"}, fake_say)
        )
    finally:
        war_room_mod._default_handler = original_handler

    assert posted == [("3%.", "T1")]


def test_message_handler_attributes_a_follow_up_to_the_asker():
    """A follow-up question carries who asked, like the gate commands do.

    Without it the question's Langfuse trace has `userId: null`, and over
    Slack — the only channel — there is nothing else to attribute it with.
    """
    from sre_agent.war_room import ThreadRef, WarRoomRegistry
    import sre_agent.war_room as war_room_mod

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)
    handler = app.handlers["message"]

    async def fake_users_info(user):
        assert user == "U42"
        return {"user": {"profile": {"email": "oncall@example.com"}}}

    app.client = types.SimpleNamespace(users_info=fake_users_info)

    async def fake_say(text, thread_ts):
        pass

    seen = {}

    async def fake_default_handler(text, incident_id, asker_email=None):
        seen["asker_email"] = asker_email
        return {"status": "RESPONDED", "response": "3%."}

    original_handler = war_room_mod._default_handler
    war_room_mod._default_handler = fake_default_handler
    try:
        asyncio.run(
            handler(
                {"channel": "C1", "thread_ts": "T1", "user": "U42",
                 "text": "why does this need approval?"},
                fake_say,
            )
        )
    finally:
        war_room_mod._default_handler = original_handler

    assert seen["asker_email"] == "oncall@example.com"


def test_message_handler_routes_gate_command_via_resolved_email():
    from sre_agent.war_room import ThreadRef, WarRoomRegistry
    import sre_agent.war_room as war_room_mod

    registry = WarRoomRegistry()
    registry.open("inc-1", ThreadRef("C1", "T1"))
    app = _build_real_app_with_registry(registry)
    handler = app.handlers["message"]

    async def fake_users_info(user):
        assert user == "U42"
        return {"user": {"profile": {"email": "oncall@example.com"}}}

    app.client = types.SimpleNamespace(users_info=fake_users_info)

    posted = []

    async def fake_say(text, thread_ts):
        posted.append((text, thread_ts))

    seen = {}

    async def fake_decide_gate_for_incident(incident_id, gate, approved, approver_email):
        seen["incident_id"] = incident_id
        seen["gate"] = gate
        seen["approved"] = approved
        seen["approver_email"] = approver_email
        return {"mode": "gate_decision", "status": "ok", "message": "gate decided"}

    original = war_room_mod._decide_gate_for_incident
    war_room_mod._decide_gate_for_incident = fake_decide_gate_for_incident
    try:
        asyncio.run(
            handler({"channel": "C1", "thread_ts": "T1", "user": "U42", "text": "approve start-fix"}, fake_say)
        )
    finally:
        war_room_mod._decide_gate_for_incident = original

    assert seen == {
        "incident_id": "inc-1",
        "gate": "start_fix",
        "approved": True,
        "approver_email": "oncall@example.com",
    }
    assert posted == [("gate decided", "T1")]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
