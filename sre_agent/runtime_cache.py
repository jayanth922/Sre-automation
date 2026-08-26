"""Concurrency-safe cache of tenant-bound agent runtimes."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .execution_context import ExecutionContext


@dataclass
class RuntimeBundle:
    context: ExecutionContext
    graph: Any
    tools: list[Any]
    mcp_client: Any = None


async def _close_client(client: Any) -> None:
    if client is None:
        return
    for name in ("aclose", "close"):
        method = getattr(client, name, None)
        if method is None:
            continue
        result = method()
        if asyncio.iscoroutine(result):
            await result
        return


class AgentRuntimeCache:
    def __init__(self, max_size: int = 32):
        if max_size < 1:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self._entries: OrderedDict[str, RuntimeBundle] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_or_create(
        self,
        context: ExecutionContext,
        factory: Callable[[ExecutionContext], Awaitable[RuntimeBundle]],
    ) -> RuntimeBundle:
        key = context.fingerprint()
        async with self._guard:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
            key_lock = self._locks.setdefault(key, asyncio.Lock())

        async with key_lock:
            async with self._guard:
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached

            try:
                bundle = await factory(context)
            except Exception:
                async with self._guard:
                    if self._locks.get(key) is key_lock:
                        self._locks.pop(key, None)
                raise
            evicted: list[RuntimeBundle] = []
            async with self._guard:
                # A context change for a cluster invalidates its older endpoint
                # set immediately instead of waiting for general LRU pressure.
                stale_keys = [
                    old_key
                    for old_key, old_bundle in self._entries.items()
                    if old_bundle.context.cluster_id == context.cluster_id
                    and old_key != key
                ]
                for stale_key in stale_keys:
                    evicted.append(self._entries.pop(stale_key))

                self._entries[key] = bundle
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_size:
                    _, old_bundle = self._entries.popitem(last=False)
                    evicted.append(old_bundle)
                self._locks.pop(key, None)

            for old_bundle in evicted:
                await _close_client(old_bundle.mcp_client)
            return bundle

    async def close_all(self) -> None:
        async with self._guard:
            bundles = list(self._entries.values())
            self._entries.clear()
            self._locks.clear()
        for bundle in bundles:
            await _close_client(bundle.mcp_client)

    async def cached(self, context: ExecutionContext) -> Optional[RuntimeBundle]:
        key = context.fingerprint()
        async with self._guard:
            return self._entries.get(key)
