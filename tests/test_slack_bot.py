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
    assert "fold that into" in sb.format_reply({"mode": "steer"})
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
    assert "fold that into" in reply


def test_process_mention_query_path():
    async def respond(_):
        pass

    async def fake_handler(text, incident_id):
        return {"mode": "query", "valid": True, "executed": True, "promql": "rate(errors[5m])", "data": 0.03}

    reply = asyncio.run(sb.process_mention("<@U1> checkout error rate", None, respond, handler=fake_handler))
    assert "rate(errors[5m])" in reply


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

    async def fake_default_handler(text, incident_id):
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
