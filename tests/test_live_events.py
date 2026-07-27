#!/usr/bin/env python3
"""Unit tests for the live event bus (realtime backbone). In-memory, single loop."""

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

    assert asyncio.run(scenario())["n"] == 1


def test_channel_isolation():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub_a = bus.subscribe("a")
        sub_b = bus.subscribe("b")
        await bus.publish("a", {"who": "a"})
        got_a = await asyncio.wait_for(sub_a.get(), timeout=1)
        # b must not receive a's event
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
        await le.publish_incident_event("inc-1", "act_report", {"severity": "SEV2"}, bus=bus)
        return await sub.get()

    ev = asyncio.run(scenario())
    assert ev["type"] == "act_report"
    assert ev["incident_id"] == "inc-1"
    assert ev["payload"]["severity"] == "SEV2"
    assert ev["ts"]


def test_insight_helper():
    async def scenario():
        bus = le.InMemoryEventBus()
        sub = bus.subscribe(le.INSIGHTS_CHANNEL)
        await le.publish_insight({"error_rate": 0.02}, bus=bus)
        return await sub.get()

    ev = asyncio.run(scenario())
    assert ev["type"] == "insight" and ev["payload"]["error_rate"] == 0.02


def test_factory_default_is_in_memory(monkeypatch):
    monkeypatch.delenv("LIVE_BUS_BACKEND", raising=False)
    le._BUS = None
    assert isinstance(le.get_event_bus(), le.InMemoryEventBus)


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
