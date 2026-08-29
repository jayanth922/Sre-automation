#!/usr/bin/env python3
"""Unit tests for the live event bus (realtime backbone)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "live_events.py"
_spec = importlib.util.spec_from_file_location("live_events", _MODULE_PATH)
le = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = le
_spec.loader.exec_module(le)


def test_publish_reaches_subscriber():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe("c1")
        await bus.publish("c1", {"n": 1})
        return await sub.get()

    event = asyncio.run(scenario())
    assert event["n"] == 1
    assert event["v"] == le.EVENT_SCHEMA_VERSION
    assert event["cursor"]


def test_channel_isolation():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub_a = bus.subscribe("a")
        sub_b = bus.subscribe("b")
        await bus.publish("a", {"who": "a"})
        got_a = await asyncio.wait_for(sub_a.get(), timeout=1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub_b.get(), timeout=0.1)
        return got_a

    assert asyncio.run(scenario())["who"] == "a"


def test_multiple_subscribers_all_receive():
    async def scenario():
        bus = le.InMemoryEventBus()
        s1, s2 = bus.subscribe("c"), bus.subscribe("c")
        await bus.publish("c", {"x": 42})
        return (await s1.get())["x"], (await s2.get())["x"]

    assert asyncio.run(scenario()) == (42, 42)


def test_close_unsubscribes():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe("c")
        assert bus.subscriber_count("c") == 1
        sub.close()
        return bus.subscriber_count("c")

    assert asyncio.run(scenario()) == 0


def test_publish_incident_event_helper():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe(le.incident_channel("inc-1"))
        await le.publish_incident_event(
            "inc-1", "act_report", {"severity": "SEV2"}, bus=bus, org_id="org-a"
        )
        return await sub.get()

    ev = asyncio.run(scenario())
    assert ev["type"] == "act_report"
    assert ev["incident_id"] == "inc-1"
    assert ev["payload"]["severity"] == "SEV2"
    assert ev["org_id"] == "org-a"
    assert ev["v"] == 1
    assert ev["ts"]


def test_insight_helper():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe(le.INSIGHTS_CHANNEL)
        await le.publish_insight({"error_rate": 0.02}, bus=bus, org_id="org-a")
        return await sub.get()

    ev = asyncio.run(scenario())
    assert ev["type"] == "insight" and ev["payload"]["error_rate"] == 0.02
    assert ev["org_id"] == "org-a"


def test_factory_default_is_in_memory(monkeypatch):
    monkeypatch.delenv("LIVE_BUS_BACKEND", raising=False)
    le.reset_event_bus_for_tests()
    assert isinstance(le.get_event_bus(), le.InMemoryEventBus)


def test_factory_prefers_redis_streams(monkeypatch):
    monkeypatch.setenv("LIVE_BUS_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://example:6379")
    le.reset_event_bus_for_tests()
    bus = le.get_event_bus()
    assert isinstance(bus, le.RedisStreamEventBus)


def test_async_iteration():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe("c")
        await bus.publish("c", {"i": 1})
        await bus.publish("c", {"i": 2})
        out = []
        async for ev in sub:
            out.append(ev["i"])
            if len(out) == 2:
                break
        return out

    assert asyncio.run(scenario()) == [1, 2]


def test_cursor_replay_resumes_after_disconnect():
    async def scenario():
        bus = le.InMemoryEventBus()
        first = await bus.publish(
            "incidents", le.LiveEvent("opened", {"n": 1}, org_id="org-a").to_dict()
        )
        second = await bus.publish(
            "incidents", le.LiveEvent("opened", {"n": 2}, org_id="org-a").to_dict()
        )
        replayed = await bus.replay("incidents", first["cursor"])
        return [event["payload"]["n"] for event in replayed], second["cursor"]

    nums, cursor = asyncio.run(scenario())
    assert nums == [2]
    assert cursor.startswith("mem-")


def test_tenant_envelopes_are_isolated_by_org_id():
    async def scenario():
        bus = le.InMemoryEventBus()
        await bus.publish(
            "incidents",
            le.LiveEvent("opened", {"who": "a"}, org_id="org-a").to_dict(),
        )
        await bus.publish(
            "incidents",
            le.LiveEvent("opened", {"who": "b"}, org_id="org-b").to_dict(),
        )
        replayed = await bus.replay("incidents")
        visible_a = [event for event in replayed if le.event_org_id(event) == "org-a"]
        visible_b = [event for event in replayed if le.event_org_id(event) == "org-b"]
        return [e["payload"]["who"] for e in visible_a], [
            e["payload"]["who"] for e in visible_b
        ]

    a, b = asyncio.run(scenario())
    assert a == ["a"]
    assert b == ["b"]


def test_backpressure_drops_when_subscriber_queue_full():
    async def scenario():
        bus = le.InMemoryEventBus(max_queue=1, history=10)
        sub = bus.subscribe("c")
        await bus.publish("c", {"n": 1})
        await bus.publish("c", {"n": 2})  # dropped for live sub, retained in history
        live = await sub.get()
        replayed = await bus.replay("c")
        return live["n"], [event["n"] for event in replayed]

    live_n, history = asyncio.run(scenario())
    assert live_n == 1
    assert history == [1, 2]


def test_lifecycle_helper_uses_versioned_incidents_channel():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe(le.INCIDENTS_LIFECYCLE_CHANNEL)
        await le.publish_lifecycle_event(
            "opened",
            incident_id="inc-9",
            alert_name="HighCPU",
            org_id="org-z",
            bus=bus,
        )
        return await sub.get()

    ev = asyncio.run(scenario())
    assert ev["type"] == "opened"
    assert ev["org_id"] == "org-z"
    assert ev["v"] == le.EVENT_SCHEMA_VERSION
    assert ev["incident_id"] == "inc-9"


def test_active_runtime_publishes_lifecycle_from_canonical_path():
    source = Path("sre_agent/agent_runtime.py").read_text()
    assert "publish_lifecycle_event" in source
    assert 'cursor = websocket.query_params.get("cursor")' in source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
