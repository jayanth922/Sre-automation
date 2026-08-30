#!/usr/bin/env python3
"""Durable investigation worker lease regression tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from sre_agent import job_worker


@pytest.mark.asyncio
async def test_lease_renewer_heartbeats_repeatedly(monkeypatch):
    stop = asyncio.Event()
    calls = []

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_heartbeat(db, job_id, *, worker_id, lease_seconds):
        calls.append((job_id, worker_id, lease_seconds))
        if len(calls) == 2:
            stop.set()

    monkeypatch.setattr(
        job_worker.database, "AsyncSessionLocal", lambda: FakeSessionContext()
    )
    monkeypatch.setattr(job_worker, "heartbeat_job", fake_heartbeat)
    job_id = uuid.uuid4()

    await job_worker._renew_job_lease(
        job_id,
        worker_id="worker-a",
        lease_seconds=60,
        stop=stop,
        renewal_interval=0.001,
    )

    assert calls == [
        (job_id, "worker-a", 60),
        (job_id, "worker-a", 60),
    ]
