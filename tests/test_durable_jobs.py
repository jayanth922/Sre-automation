#!/usr/bin/env python3
"""Tests for R02 durable leased investigation jobs."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.durable_jobs import (
    DurableJobError,
    InMemoryDurableJobStore,
    encode_investigation_payload,
    investigation_idempotency_key,
)


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_duplicate_alert_delivery_reuses_active_job():
    store = InMemoryDurableJobStore()
    cluster_id, org_id, incident_id = _ids()
    key = investigation_idempotency_key(incident_id)
    payload = encode_investigation_payload(
        incident_id=incident_id,
        cluster_id=cluster_id,
        alert_name="CheckoutHighErrorRate",
    )

    first = store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=incident_id,
        job_type="investigation",
        payload=payload,
        idempotency_key=key,
    )
    second = store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=incident_id,
        job_type="investigation",
        payload=payload,
        idempotency_key=key,
    )

    assert first.id == second.id
    assert len(store.all()) == 1


def test_single_owner_lease_and_expired_lease_reclaim():
    store = InMemoryDurableJobStore()
    cluster_id, org_id, incident_id = _ids()
    job = store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=incident_id,
        job_type="investigation",
        payload={"handler": "run_graph_background_saas"},
        idempotency_key=investigation_idempotency_key(incident_id),
        max_attempts=2,
    )

    claimed = store.claim(worker_id="worker-a", lease_seconds=30)
    assert len(claimed) == 1
    assert claimed[0].lease_owner == "worker-a"
    assert store.claim(worker_id="worker-b") == []

    with pytest.raises(DurableJobError):
        store.heartbeat(job.id, worker_id="worker-b")

    expired = datetime.now(timezone.utc) + timedelta(seconds=1)
    claimed[0].lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    reclaimed = store.reclaim_expired(now=expired)
    assert len(reclaimed) == 1
    assert reclaimed[0].status == "pending"

    again = store.claim(worker_id="worker-b", lease_seconds=30)
    assert len(again) == 1
    assert again[0].lease_owner == "worker-b"
    assert again[0].attempt_count == 2


def test_retries_are_bounded_then_dead_letter():
    store = InMemoryDurableJobStore()
    cluster_id, org_id, incident_id = _ids()
    store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=incident_id,
        job_type="investigation",
        payload={},
        idempotency_key=investigation_idempotency_key(incident_id),
        max_attempts=2,
    )

    first = store.claim(worker_id="w1")[0]
    store.fail(first.id, worker_id="w1", error="boom-1")
    assert store.get(first.id).status == "pending"

    second = store.claim(worker_id="w1")[0]
    store.fail(second.id, worker_id="w1", error="boom-2")
    assert store.get(second.id).status == "dead_letter"


def test_cancellation_is_honored_for_pending_and_running():
    store = InMemoryDurableJobStore()
    cluster_id, org_id, incident_id = _ids()
    pending = store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=incident_id,
        job_type="investigation",
        payload={},
        idempotency_key=investigation_idempotency_key(incident_id),
    )
    store.request_cancel(pending.id)
    assert store.get(pending.id).status == "cancelled"
    assert store.claim(worker_id="w1") == []

    other_incident = uuid.uuid4()
    running = store.enqueue(
        cluster_id=cluster_id,
        organization_id=org_id,
        incident_id=other_incident,
        job_type="investigation",
        payload={},
        idempotency_key=investigation_idempotency_key(other_incident),
    )
    claimed = store.claim(worker_id="w1")[0]
    store.request_cancel(claimed.id)
    with pytest.raises(DurableJobError, match="cancellation"):
        store.heartbeat(claimed.id, worker_id="w1")
    store.fail(claimed.id, worker_id="w1", error="cancelled")
    assert store.get(running.id).status == "cancelled"


def test_per_tenant_fairness_prefers_different_organizations():
    store = InMemoryDurableJobStore()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    cluster_a, cluster_b = uuid.uuid4(), uuid.uuid4()

    for _ in range(3):
        incident = uuid.uuid4()
        store.enqueue(
            cluster_id=cluster_a,
            organization_id=org_a,
            incident_id=incident,
            job_type="investigation",
            payload={},
            idempotency_key=investigation_idempotency_key(incident),
        )
    incident_b = uuid.uuid4()
    store.enqueue(
        cluster_id=cluster_b,
        organization_id=org_b,
        incident_id=incident_b,
        job_type="investigation",
        payload={},
        idempotency_key=investigation_idempotency_key(incident_b),
    )

    claimed = store.claim(worker_id="w1", limit=2)
    orgs = {job.organization_id for job in claimed}
    assert orgs == {org_a, org_b}
