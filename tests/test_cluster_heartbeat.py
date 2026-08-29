#!/usr/bin/env python3
"""Truthful cluster heartbeat evaluation tests."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.models import ClusterStatus
from sre_agent.cluster_heartbeat import evaluate_heartbeat, heartbeat_payload


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setenv("CLUSTER_HEARTBEAT_ONLINE_SECONDS", "60")
    monkeypatch.setenv("CLUSTER_HEARTBEAT_DEGRADED_SECONDS", "180")
    monkeypatch.setenv("CLUSTER_HEARTBEAT_STALE_SECONDS", "600")


def test_never_seen_is_offline():
    result = evaluate_heartbeat(None)
    assert result.status is ClusterStatus.OFFLINE
    assert result.reason == "never_seen"
    assert result.age_seconds is None


def test_fresh_observed_heartbeat_is_online():
    now = datetime.now(timezone.utc)
    result = evaluate_heartbeat(now - timedelta(seconds=10), source="edge", now=now)
    assert result.status is ClusterStatus.ONLINE
    assert result.source == "edge"
    assert result.age_seconds == pytest.approx(10, abs=0.01)


def test_ages_through_degraded_stale_offline():
    now = datetime.now(timezone.utc)
    assert (
        evaluate_heartbeat(
            now - timedelta(seconds=90), source="alertmanager", now=now
        ).status
        is ClusterStatus.DEGRADED
    )
    assert (
        evaluate_heartbeat(
            now - timedelta(seconds=300), source="alertmanager", now=now
        ).status
        is ClusterStatus.STALE
    )
    assert (
        evaluate_heartbeat(
            now - timedelta(seconds=900), source="alertmanager", now=now
        ).status
        is ClusterStatus.OFFLINE
    )


def test_payload_exposes_source_reason_and_age():
    now = datetime.now(timezone.utc)
    payload = heartbeat_payload(
        now - timedelta(seconds=12),
        source="alertmanager",
        reason="alertmanager_webhook",
        now=now,
    )
    assert payload["status"] == "online"
    assert payload["heartbeat_source"] == "alertmanager"
    assert payload["heartbeat_reason"] == "alertmanager_webhook"
    assert payload["age_seconds"] == pytest.approx(12, abs=0.01)
    assert payload["last_heartbeat"] is not None


def test_agent_runtime_no_longer_fabricates_global_online_heartbeats():
    from pathlib import Path

    source = Path("sre_agent/agent_runtime.py").read_text()
    assert "_heartbeat_reconcile_loop" in source
    assert "reconcile_cluster_heartbeats" in source
    assert "Keep all clusters marked online" not in source
    # Synthetic bulk ONLINE refresh must not return.
    assert (
        "status=models.ClusterStatus.ONLINE,\n                        last_heartbeat=datetime.now(timezone.utc)"
        not in source
    )
