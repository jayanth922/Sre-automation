#!/usr/bin/env python3
"""Unit tests for the two-way war room (design slice #2). In-memory + fakes."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "sre_agent"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


le = _load("live_events")
# war_room imports `from .live_events import ...`; load it as a package instead.
sys.path.insert(0, str(_BASE.parent))
from sre_agent.war_room import (  # noqa: E402
    ThreadRef,
    WarRoomRegistry,
    forward_events,
    format_event_for_slack,
    route_thread_reply,
)
from sre_agent.live_events import InMemoryEventBus, publish_incident_event  # noqa: E402


# ── registry ─────────────────────────────────────────────────────────────────
def test_registry_bidirectional():
    reg = WarRoomRegistry()
    t = ThreadRef("C1", "111.222")
    reg.open("inc-1", t)
    assert reg.thread_for("inc-1") == t
    assert reg.incident_for(t) == "inc-1"
    assert reg.is_war_room(t)
    reg.close("inc-1")
    assert reg.thread_for("inc-1") is None
    assert not reg.is_war_room(t)


# ── event formatting ───────────────────────────────────────────────────────────
def test_format_surfaces_plan_skips_noise():
    plan = {"type": "timeline", "payload": {"event_type": "plan", "title": "Supervisor", "content": "Investigation plan…"}}
    noise = {"type": "timeline", "payload": {"event_type": "thought", "title": "x", "content": "y"}}
    insight = {"type": "insight", "payload": {"error_rate": 0.1}}
    assert "Supervisor" in format_event_for_slack(plan)
    assert format_event_for_slack(noise) is None
    assert format_event_for_slack(insight) is None


# ── outbound: bus → Slack thread ──────────────────────────────────────────────
def test_forward_events_posts_surfaced_events():
    async def scenario():
        bus = InMemoryEventBus()
        reg = WarRoomRegistry()
        reg.open("inc-1", ThreadRef("C1", "T1"))
        posts = []

        async def poster(thread, text):
            posts.append((thread, text))

        task = asyncio.create_task(forward_events("inc-1", poster, bus=bus, registry=reg, max_events=1))
        await asyncio.sleep(0.01)  # let it subscribe
        await publish_incident_event("inc-1", "timeline",
                                     {"event_type": "act", "title": "Executor", "content": "restarted pod"}, bus=bus)
        await asyncio.wait_for(task, timeout=1)
        return posts

    posts = asyncio.run(scenario())
    assert len(posts) == 1
    thread, text = posts[0]
    assert thread == ThreadRef("C1", "T1")
    assert "Executor" in text and "restarted pod" in text


# ── inbound: thread reply routing ─────────────────────────────────────────────
def test_route_steer_feeds_checkpoint_and_acks():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        steers, posts = [], []

        async def poster(thread, text): posts.append(text)
        async def steer_sink(incident_id, text): steers.append((incident_id, text))
        async def handler(text, incident_id): return {"mode": "steer"}

        await route_thread_reply("focus on logs", t, reg, poster, steer_sink, handler=handler)
        return steers, posts

    steers, posts = asyncio.run(scenario())
    assert steers == [("inc-1", "focus on logs")]     # pushed to the checkpoint queue
    assert posts and "folding that into" in posts[0]  # acked in-thread


def test_route_query_answers_in_thread():
    async def scenario():
        reg = WarRoomRegistry()
        t = ThreadRef("C1", "T1")
        reg.open("inc-1", t)
        posts = []

        async def poster(thread, text): posts.append(text)
        async def steer_sink(i, x): raise AssertionError("query should not steer")
        async def handler(text, incident_id):
            return {"mode": "query", "valid": True, "executed": True, "promql": "rate(errors[5m])", "data": 0.03}

        await route_thread_reply("error rate?", t, reg, poster, steer_sink, handler=handler)
        return posts

    posts = asyncio.run(scenario())
    assert posts and "rate(errors[5m])" in posts[0]


def test_route_ignores_non_war_room_thread():
    async def scenario():
        reg = WarRoomRegistry()  # empty
        posts = []
        async def poster(thread, text): posts.append(text)
        async def steer_sink(i, x): raise AssertionError("should not steer")
        async def handler(text, incident_id): raise AssertionError("should not handle")
        res = await route_thread_reply("hello", ThreadRef("C9", "T9"), reg, poster, steer_sink, handler=handler)
        return res, posts

    res, posts = asyncio.run(scenario())
    assert res["mode"] == "ignored" and posts == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
