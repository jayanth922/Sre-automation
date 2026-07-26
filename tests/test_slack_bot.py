#!/usr/bin/env python3
"""Unit tests for the Slack transport (project #3). No Slack/MCP needed."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Load the module by path (its lazy slack_bolt import means it loads fine without it).
_PKG = Path(__file__).resolve().parents[1] / "sre_agent" / "integrations"
_spec = importlib.util.spec_from_file_location("slack_bot", _PKG / "slack_bot.py")
sb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sb
_spec.loader.exec_module(sb)


def test_format_reply_modes():
    assert "SRE agent" in sb.format_reply({"mode": "greeting"})
    assert "fold that into" in sb.format_reply({"mode": "steer"})
    q = sb.format_reply({"mode": "query", "valid": True, "executed": True, "promql": "sum(x)", "data": [1]})
    assert "sum(x)" in q and "[1]" in q


def test_format_reply_invalid_query():
    r = sb.format_reply({"mode": "query", "valid": False, "error": "bad metric"})
    assert "couldn't turn that into a safe query" in r
    assert "bad metric" in r


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
