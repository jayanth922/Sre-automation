"""Missed Alertmanager clears are recovered only from durable source evidence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import models
from sre_agent.alert_lifecycle_reconciler import (
    ABSENCE_EVENT,
    RESOLVED_EVENT,
    AlertIdentity,
    AlertRuleSnapshot,
    alert_identity_from_job_payload,
    observe_alert_source,
)

UTC = timezone.utc


def _identity() -> AlertIdentity:
    return AlertIdentity(
        alertname="InventorySlowQueries",
        service="inventory-service",
        labels={
            "alertname": "InventorySlowQueries",
            "service": "inventory-service",
            "severity": "warning",
        },
    )


def _rule(*, health="ok", alerts=None):
    return {
        "type": "alerting",
        "name": "InventorySlowQueries",
        "health": health,
        "alerts": [] if alerts is None else alerts,
    }


def test_only_durable_alertmanager_jobs_supply_reconciliation_identity():
    payload = {
        "triggered_by": "alertmanager_webhook",
        "alert_name": "InventorySlowQueries",
        "alert_labels": {"service": "inventory-service", "query": "slow-1"},
    }
    assert alert_identity_from_job_payload(json.dumps(payload)) == AlertIdentity(
        alertname="InventorySlowQueries",
        service="inventory-service",
        labels=payload["alert_labels"],
    )
    payload["triggered_by"] = "manual"
    assert alert_identity_from_job_payload(payload) is None
    assert alert_identity_from_job_payload("not-json") is None
    assert alert_identity_from_job_payload("[]") is None


@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (AlertRuleSnapshot(False, reason="network_failed"), "network_failed"),
        (AlertRuleSnapshot(True, rules=()), "alert_rule_missing"),
        (
            AlertRuleSnapshot(True, rules=(_rule(health="err"),)),
            "alert_rule_unhealthy",
        ),
    ],
)
def test_unavailable_or_unhealthy_rule_state_never_means_clear(snapshot, reason):
    observation = observe_alert_source(snapshot, _identity())
    assert observation.state == "unavailable"
    assert observation.reason == reason


def test_healthy_rule_state_distinguishes_matching_active_series_from_absence():
    absent = observe_alert_source(
        AlertRuleSnapshot(True, rules=(_rule(),)), _identity()
    )
    assert absent.state == "absent"

    another_service = observe_alert_source(
        AlertRuleSnapshot(
            True,
            rules=(_rule(alerts=[{"labels": {"service": "checkout"}}]),),
        ),
        _identity(),
    )
    assert another_service.state == "absent"

    active = observe_alert_source(
        AlertRuleSnapshot(
            True,
            rules=(
                _rule(alerts=[{"labels": {"service": "inventory-service"}}]),
            ),
        ),
        _identity(),
    )
    assert active.state == "firing"


@pytest.mark.asyncio
async def test_process_restart_uses_durable_absence_and_preserves_clear_boundary(
    monkeypatch,
):
    """The first snapshot survives process death; the second closes only the
    lifecycle, withdraws authority through Task #40's external-clear path, and
    is not replayed by a later sweep.
    """
    from backend import crud, database
    from sre_agent import alert_lifecycle_reconciler as reconciler
    from sre_agent import approval_flow

    first_seen = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    incident = SimpleNamespace(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        title="[inventory-service] InventorySlowQueries",
        status=models.IncidentStatus.INVESTIGATED,
        created_at=first_seen - timedelta(hours=1),
        summary="investigated; no remediation was authorized",
    )
    cluster = SimpleNamespace(
        id=incident.cluster_id,
        org_id=uuid.uuid4(),
        prometheus_url="http://prometheus.test",
    )
    state = {
        "latest": None,
        "resolved_events": [],
        "external_clears": [],
        "sessions": 0,
    }

    class Result:
        rowcount = 1

    class FakeDB:
        async def execute(self, _statement):
            incident.status = models.IncidentStatus.RESOLVED
            return Result()

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def __aenter__(self):
            state["sessions"] += 1
            return self

        async def __aexit__(self, *_args):
            return False

    async def load_candidates(_db):
        if incident.status == models.IncidentStatus.RESOLVED:
            return []
        return [(incident, cluster)]

    async def load_identity(_db, _incident_id):
        return _identity()

    async def latest_event(_db, _incident_id):
        return state["latest"]

    async def record_event(_db, _incident, _identity_value, *, event_type, now):
        event = SimpleNamespace(event_type=event_type, created_at=now)
        state["latest"] = event
        return event

    async def healthy_inactive(_url):
        return AlertRuleSnapshot(True, rules=(_rule(),))

    async def find_active(_db, _cluster_id, _title):
        return (
            incident
            if incident.status != models.IncidentStatus.RESOLVED
            else None
        )

    async def timeline(_db, _incident_id, **values):
        assert values["event_type"] == RESOLVED_EVENT
        state["resolved_events"].append(values)
        state["latest"] = SimpleNamespace(
            event_type=RESOLVED_EVENT, created_at=first_seen + timedelta(minutes=6)
        )

    async def external_clear(
        resolved_incident,
        organization_id,
        cluster_id,
        *,
        source_label="Alertmanager",
    ):
        assert resolved_incident is incident
        assert organization_id == str(cluster.org_id)
        assert cluster_id == str(cluster.id)
        state["external_clears"].append(source_label)

    async def human_resolution_must_not_run(*_args, **_kwargs):
        raise AssertionError("source absence is not a human stop or remediation success")

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: FakeDB())
    monkeypatch.setattr(reconciler, "_load_candidates", load_candidates)
    monkeypatch.setattr(reconciler, "_load_alert_identity", load_identity)
    monkeypatch.setattr(reconciler, "_latest_source_event", latest_event)
    monkeypatch.setattr(reconciler, "_record_source_event", record_event)
    monkeypatch.setattr(crud, "find_active_incident_by_title", find_active)
    monkeypatch.setattr(crud, "create_incident_timeline_event", timeline)
    monkeypatch.setattr(
        approval_flow, "fire_external_alert_clear_side_effects", external_clear
    )
    monkeypatch.setattr(
        approval_flow, "fire_resolution_side_effects", human_resolution_must_not_run
    )

    # Process 1 sees one healthy absence and persists it, but cannot close.
    first = await reconciler.reconcile_missed_alert_resolutions(
        now=first_seen,
        confirm_after=timedelta(minutes=5),
        snapshot_loader=healthy_inactive,
    )
    assert first == []
    assert state["latest"].event_type == ABSENCE_EVENT
    assert incident.status == models.IncidentStatus.INVESTIGATED
    assert state["external_clears"] == []

    # Process 2 starts with only durable timeline state. A later independent
    # healthy snapshot closes the lifecycle through the external-clear path.
    second = await reconciler.reconcile_missed_alert_resolutions(
        now=first_seen + timedelta(minutes=6),
        confirm_after=timedelta(minutes=5),
        snapshot_loader=healthy_inactive,
    )
    assert len(second) == 1
    assert second[0].previous_status == "investigated"
    assert second[0].new_status == "resolved"
    assert incident.status == models.IncidentStatus.RESOLVED
    assert state["external_clears"] == ["Prometheus rule reconciliation"]
    assert state["resolved_events"][0]["payload"]["remediation_verified"] is False
    assert "does not prove" in state["resolved_events"][0]["content"]

    # A later sweep has no active candidate and cannot replay closure effects.
    third = await reconciler.reconcile_missed_alert_resolutions(
        now=first_seen + timedelta(minutes=12),
        confirm_after=timedelta(minutes=5),
        snapshot_loader=healthy_inactive,
    )
    assert third == []
    assert state["external_clears"] == ["Prometheus rule reconciliation"]
    assert state["sessions"] == 3
