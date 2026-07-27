#!/usr/bin/env python3
"""
Live event bus — the realtime backbone for streaming insights and the incident
conversation to the dashboard (and any other subscriber).

This is slice #1 of the "continuous live" design: producers (the always-on
monitor, the agent's timeline, inbound chat) publish events to channels; the
dashboard subscribes over a WebSocket and receives them pushed — no polling.

- `InMemoryEventBus` — asyncio-queue fan-out; the default and fully unit-testable.
- `RedisEventBus` — Redis pub/sub for multi-process/multi-replica deployments
  (Redis is already in the stack). Lazy import so this module stays light.

Channels: ``incident:{id}`` for one incident's live conversation, ``insights``
for the global app/cluster health stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class LiveEvent:
    type: str                       # timeline | act_report | insight | chat | status
    payload: Dict[str, Any] = field(default_factory=dict)
    incident_id: Optional[str] = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def incident_channel(incident_id: str) -> str:
    return f"incident:{incident_id}"


INSIGHTS_CHANNEL = "insights"


# ── In-memory bus (default) ──────────────────────────────────────────────────
class _Subscription:
    def __init__(self, bus: "InMemoryEventBus", channel: str, queue: "asyncio.Queue[Dict[str, Any]]"):
        self._bus = bus
        self._channel = channel
        self._queue = queue

    async def get(self) -> Dict[str, Any]:
        return await self._queue.get()

    def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Dict[str, Any]]:
        while True:
            yield await self._queue.get()

    def close(self) -> None:
        self._bus._unsubscribe(self._channel, self._queue)


class EventBus:
    async def publish(self, channel: str, event: Dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def subscribe(self, channel: str):  # pragma: no cover
        raise NotImplementedError


class InMemoryEventBus(EventBus):
    def __init__(self, max_queue: int = 1000) -> None:
        self._subs: Dict[str, Set["asyncio.Queue[Dict[str, Any]]"]] = {}
        self._max = max_queue

    async def publish(self, channel: str, event: Dict[str, Any]) -> None:
        for q in list(self._subs.get(channel, set())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"live_events: subscriber queue full on {channel}; dropping event")

    def subscribe(self, channel: str) -> _Subscription:
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=self._max)
        self._subs.setdefault(channel, set()).add(q)
        return _Subscription(self, channel, q)

    def _unsubscribe(self, channel: str, q: "asyncio.Queue[Dict[str, Any]]") -> None:
        subs = self._subs.get(channel)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(channel, None)

    def subscriber_count(self, channel: str) -> int:
        return len(self._subs.get(channel, set()))


# ── Redis bus (prod) ─────────────────────────────────────────────────────────
class RedisEventBus(EventBus):
    """Redis pub/sub bus. Requires ``redis`` (already a project dependency)."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as redis  # lazy
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, channel: str, event: Dict[str, Any]) -> None:
        r = await self._redis()
        await r.publish(channel, json.dumps(event))

    def subscribe(self, channel: str) -> "_RedisSubscription":
        return _RedisSubscription(self, channel)


class _RedisSubscription:
    def __init__(self, bus: RedisEventBus, channel: str):
        self._bus = bus
        self._channel = channel
        self._pubsub = None

    async def _ensure(self):
        if self._pubsub is None:
            r = await self._bus._redis()
            self._pubsub = r.pubsub()
            await self._pubsub.subscribe(self._channel)
        return self._pubsub

    async def get(self) -> Dict[str, Any]:
        ps = await self._ensure()
        async for message in ps.listen():
            if message.get("type") == "message":
                return json.loads(message["data"])
        return {}

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        ps = await self._ensure()
        async for message in ps.listen():
            if message.get("type") == "message":
                yield json.loads(message["data"])

    def close(self) -> None:
        # best-effort; the pubsub is cleaned up when the connection closes
        self._pubsub = None


# ── Factory + convenience ────────────────────────────────────────────────────
_BUS: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        backend = os.getenv("LIVE_BUS_BACKEND", "memory").lower()
        if backend == "redis":
            try:
                _BUS = RedisEventBus(os.getenv("REDIS_URL", "redis://redis:6379"))
                logger.info("live_events: using Redis bus")
            except Exception as e:
                logger.warning(f"live_events: Redis bus unavailable ({e}); using in-memory")
                _BUS = InMemoryEventBus()
        else:
            _BUS = InMemoryEventBus()
    return _BUS


async def publish_incident_event(incident_id: str, event_type: str, payload: Dict[str, Any], bus: Optional[EventBus] = None) -> None:
    """Publish an event to an incident's live channel (best-effort, non-fatal)."""
    bus = bus or get_event_bus()
    try:
        await bus.publish(incident_channel(incident_id), LiveEvent(event_type, payload, incident_id).to_dict())
    except Exception as e:
        logger.warning(f"live_events: publish failed: {e}")


async def publish_insight(payload: Dict[str, Any], bus: Optional[EventBus] = None) -> None:
    """Publish an app/cluster health insight to the global stream (best-effort)."""
    bus = bus or get_event_bus()
    try:
        await bus.publish(INSIGHTS_CHANNEL, LiveEvent("insight", payload).to_dict())
    except Exception as e:
        logger.warning(f"live_events: insight publish failed: {e}")
