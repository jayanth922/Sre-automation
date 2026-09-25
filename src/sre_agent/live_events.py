#!/usr/bin/env python3
"""
Live event bus — realtime backbone for streaming insights and incident
conversation to the dashboard (and any other subscriber).

R09: durable, tenant-scoped delivery with versioned envelopes and replay
cursors so API replicas share one broker and reconnects resume without
dropping or leaking cross-tenant payloads.

- ``InMemoryEventBus`` — asyncio-queue fan-out + ring-buffer replay (tests/dev).
- ``RedisStreamEventBus`` — Redis Streams for multi-replica durability, cursors,
  and approximate maxlen backpressure.
- Legacy ``RedisEventBus`` (pub/sub) remains available via
  ``LIVE_BUS_BACKEND=redis_pubsub`` but cannot replay.

Channels: ``incident:{id}``, ``insights``, ``incidents`` (lifecycle). Tenant
isolation is enforced by stamping ``org_id`` on every envelope and filtering at
subscribe/WS boundaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

EVENT_SCHEMA_VERSION = 1


@dataclass
class LiveEvent:
    type: str  # timeline | act_report | insight | chat | status | opened | ...
    payload: Dict[str, Any] = field(default_factory=dict)
    incident_id: Optional[str] = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    v: int = EVENT_SCHEMA_VERSION
    org_id: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cursor: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def incident_channel(incident_id: str) -> str:
    return f"incident:{incident_id}"


def tenant_channel(org_id: str, name: str) -> str:
    """Optional explicit tenant-scoped channel name."""
    return f"tenant:{org_id}:{name}"


INSIGHTS_CHANNEL = "insights"
INCIDENTS_LIFECYCLE_CHANNEL = "incidents"


def stream_maxlen() -> int:
    return max(100, int(os.getenv("LIVE_BUS_STREAM_MAXLEN", "5000")))


def subscriber_queue_size() -> int:
    return max(10, int(os.getenv("LIVE_BUS_SUBSCRIBER_QUEUE", "1000")))


def ensure_event_envelope(
    event: Dict[str, Any],
    *,
    org_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize a published event to the versioned envelope."""
    payload = dict(event)
    payload.setdefault("v", EVENT_SCHEMA_VERSION)
    payload.setdefault("id", str(uuid.uuid4()))
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    payload.setdefault("type", "event")
    payload.setdefault("payload", {})
    if org_id and not payload.get("org_id"):
        payload["org_id"] = str(org_id)
    if channel and "channel" not in payload:
        payload["channel"] = channel
    return payload


def event_org_id(event: Dict[str, Any]) -> Optional[str]:
    raw = event.get("org_id")
    if raw:
        return str(raw)
    nested = event.get("payload") or {}
    if isinstance(nested, dict) and nested.get("org_id"):
        return str(nested["org_id"])
    return None


# ── In-memory bus (default) ──────────────────────────────────────────────────
class _Subscription:
    def __init__(
        self,
        bus: "InMemoryEventBus",
        channel: str,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ):
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
    async def publish(self, channel: str, event: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def subscribe(self, channel: str):
        raise NotImplementedError

    async def replay(
        self,
        channel: str,
        cursor: Optional[str] = None,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return durable events after ``cursor`` (exclusive), oldest first."""
        return []


class InMemoryEventBus(EventBus):
    def __init__(
        self, max_queue: Optional[int] = None, history: Optional[int] = None
    ) -> None:
        self._subs: Dict[str, Set["asyncio.Queue[Dict[str, Any]]"]] = {}
        self._max = max_queue or subscriber_queue_size()
        self._history_size = history or stream_maxlen()
        self._history: Dict[str, Deque[Tuple[str, Dict[str, Any]]]] = {}
        self._seq = 0

    def _next_cursor(self) -> str:
        self._seq += 1
        return f"mem-{self._seq}-{int(time.time() * 1000)}"

    async def publish(self, channel: str, event: Dict[str, Any]) -> Dict[str, Any]:
        envelope = ensure_event_envelope(event, channel=channel)
        cursor = self._next_cursor()
        envelope["cursor"] = cursor
        ring = self._history.setdefault(channel, deque(maxlen=self._history_size))
        ring.append((cursor, envelope))
        for q in list(self._subs.get(channel, set())):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                logger.warning(
                    "live_events: subscriber queue full on %s; dropping event (backpressure)",
                    channel,
                )
        return envelope

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

    async def replay(
        self,
        channel: str,
        cursor: Optional[str] = None,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        ring = list(self._history.get(channel, ()))
        if cursor:
            out: List[Dict[str, Any]] = []
            seen = False
            for item_cursor, event in ring:
                if not seen:
                    if item_cursor == cursor:
                        seen = True
                    continue
                out.append(event)
            return out[:limit]
        return [event for _, event in ring[-limit:]]


# ── Redis Streams bus (prod durability) ──────────────────────────────────────
class RedisStreamEventBus(EventBus):
    """Redis Streams bus with cursor replay and maxlen backpressure."""

    def __init__(self, url: str, maxlen: Optional[int] = None) -> None:
        self._url = url
        self._client = None
        self._maxlen = maxlen or stream_maxlen()

    def _stream_key(self, channel: str) -> str:
        return f"live:stream:{channel}"

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as redis  # lazy

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, channel: str, event: Dict[str, Any]) -> Dict[str, Any]:
        envelope = ensure_event_envelope(event, channel=channel)
        r = await self._redis()
        stream_id = await r.xadd(
            self._stream_key(channel),
            {"data": json.dumps(envelope)},
            maxlen=self._maxlen,
            approximate=True,
        )
        envelope["cursor"] = stream_id
        # Re-write is unnecessary for consumers that read the returned envelope;
        # live subscribers receive via XREAD using the stream id as cursor.
        await r.publish(f"live:notify:{channel}", stream_id)
        return envelope

    def subscribe(self, channel: str) -> "_RedisStreamSubscription":
        return _RedisStreamSubscription(self, channel)

    async def replay(
        self,
        channel: str,
        cursor: Optional[str] = None,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        r = await self._redis()
        # Exclusive start after cursor when provided.
        if cursor and cursor != "-":
            rows = await r.xrange(
                self._stream_key(channel), min=f"({cursor}", max="+", count=limit
            )
        else:
            rows = await r.xrange(
                self._stream_key(channel), min="-", max="+", count=limit
            )
        events: List[Dict[str, Any]] = []
        for stream_id, fields in rows:
            try:
                payload = json.loads(fields.get("data") or "{}")
            except json.JSONDecodeError:
                continue
            payload["cursor"] = stream_id
            events.append(payload)
        return events


class _RedisStreamSubscription:
    def __init__(self, bus: RedisStreamEventBus, channel: str):
        self._bus = bus
        self._channel = channel
        self._last_id = "$"
        self._closed = False

    def start_after(self, cursor: Optional[str]) -> None:
        if cursor:
            self._last_id = cursor

    async def get(self) -> Dict[str, Any]:
        async for event in self._iter():
            return event
        return {}

    def __aiter__(self):
        return self._iter()

    async def _iter(self) -> AsyncIterator[Dict[str, Any]]:
        r = await self._bus._redis()
        key = self._bus._stream_key(self._channel)
        while not self._closed:
            rows = await r.xread({key: self._last_id}, block=5000, count=10)
            if not rows:
                continue
            for _stream, messages in rows:
                for stream_id, fields in messages:
                    self._last_id = stream_id
                    try:
                        payload = json.loads(fields.get("data") or "{}")
                    except json.JSONDecodeError:
                        continue
                    payload["cursor"] = stream_id
                    yield payload

    def close(self) -> None:
        self._closed = True


# ── Legacy Redis pub/sub (no replay) ─────────────────────────────────────────
class RedisEventBus(EventBus):
    """Redis pub/sub bus. Prefer RedisStreamEventBus for durable delivery."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as redis  # lazy

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, channel: str, event: Dict[str, Any]) -> Dict[str, Any]:
        envelope = ensure_event_envelope(event, channel=channel)
        r = await self._redis()
        await r.publish(channel, json.dumps(envelope))
        return envelope

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
        self._pubsub = None


# ── Factory + convenience ────────────────────────────────────────────────────
_BUS: Optional[EventBus] = None


def reset_event_bus_for_tests() -> None:
    global _BUS
    _BUS = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        backend = os.getenv("LIVE_BUS_BACKEND", "memory").lower()
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        if backend in {"redis", "redis_stream", "streams"}:
            try:
                _BUS = RedisStreamEventBus(redis_url)
                logger.info("live_events: using Redis Streams bus")
            except Exception as e:
                logger.warning(
                    "live_events: Redis Streams unavailable (%s); using in-memory", e
                )
                _BUS = InMemoryEventBus()
        elif backend in {"redis_pubsub", "pubsub"}:
            try:
                _BUS = RedisEventBus(redis_url)
                logger.info("live_events: using Redis pub/sub bus")
            except Exception as e:
                logger.warning(
                    "live_events: Redis pub/sub unavailable (%s); using in-memory", e
                )
                _BUS = InMemoryEventBus()
        else:
            _BUS = InMemoryEventBus()
    return _BUS


async def publish_incident_event(
    incident_id: str,
    event_type: str,
    payload: Dict[str, Any],
    bus: Optional[EventBus] = None,
    *,
    org_id: Optional[str] = None,
) -> None:
    """Publish an event to an incident's live channel (best-effort, non-fatal)."""
    bus = bus or get_event_bus()
    try:
        await bus.publish(
            incident_channel(incident_id),
            LiveEvent(event_type, payload, incident_id, org_id=org_id).to_dict(),
        )
    except Exception as e:
        logger.warning(f"live_events: publish failed: {e}")


async def publish_insight(
    payload: Dict[str, Any],
    bus: Optional[EventBus] = None,
    *,
    org_id: Optional[str] = None,
) -> None:
    """Publish an app/cluster health insight to the global stream (best-effort)."""
    bus = bus or get_event_bus()
    try:
        await bus.publish(
            INSIGHTS_CHANNEL,
            LiveEvent("insight", payload, org_id=org_id).to_dict(),
        )
    except Exception as e:
        logger.warning(f"live_events: insight publish failed: {e}")


async def publish_lifecycle_event(
    event_type: str,
    *,
    incident_id: str,
    alert_name: str,
    summary: str = "",
    org_id: Optional[str] = None,
    status: Optional[str] = None,
    bus: Optional[EventBus] = None,
) -> None:
    """Publish cluster incident lifecycle (opened/resolved/status_changed)."""
    bus = bus or get_event_bus()
    payload = {
        "incident_id": str(incident_id),
        "alert_name": alert_name,
        "summary": summary or f"Investigating alert: {alert_name}",
    }
    if status:
        payload["status"] = status
    try:
        await bus.publish(
            INCIDENTS_LIFECYCLE_CHANNEL,
            LiveEvent(event_type, payload, str(incident_id), org_id=org_id).to_dict(),
        )
    except Exception as e:
        logger.warning(f"live_events: lifecycle publish failed: {e}")
