#!/usr/bin/env python3
"""A single Alertmanager group can carry firing and resolved members at once.

`InventorySlowQueries` emits one series per `query` label — seven of them — and
when a fix lands they stop firing a few scrape intervals apart. The next group
notification therefore contains both kinds, and the webhook handles the whole
payload in one loop.

Reconciling a resolved member while a sibling of the same alert is still firing
closes the incident that is tracking the condition; the next firing member in
that same payload then finds nothing to dedup against and opens a new one.
Observed live on 2026-09-14T01:38:15: two members deduped onto incident
030d0ffb, a resolved member closed it, the next firing member opened b6146c86,
a resolved member closed that, and the last firing member opened 9258aadb —
two orphan war rooms and two full investigations of a condition already being
remediated, plus the real incident marked resolved mid-verification.

These tests drive the endpoint's loop. `_reconcile_resolved_alert` is replaced
by a stand-in that does what the real one does to the incident store (closes
the active incident with that title), because the decision under test is
*whether the loop calls it at all*.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend import crud, models
from sre_agent import job_worker
from sre_agent.api.v1 import alerts as alerts_module


class FakeSession:
    def __init__(self):
        self.commits = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, params=None):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("the webhook loop must not roll back")


class IncidentStore:
    """Just enough of the incidents table: open one, find the active one."""

    def __init__(self):
        self.incidents = []
        self.reconcile_calls = []

    def active(self, title):
        for incident in reversed(self.incidents):
            if incident.title == title and incident.status in crud._ACTIVE_INCIDENT_STATUSES:
                return incident
        return None

    def open(self, title, status=models.IncidentStatus.OPEN):
        incident = SimpleNamespace(
            id=uuid.uuid4(), title=title, status=status, resolved_at=None
        )
        self.incidents.append(incident)
        return incident


def _alert(status, *, alertname="InventorySlowQueries", service="inventory-service", query="q"):
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "severity": "warning",
            "service": service,
            "query": query,
        },
        "annotations": {"summary": "slow", "description": "slow"},
        "startsAt": "2026-09-14T01:16:00Z",
        "endsAt": "2026-09-14T01:38:00Z" if status == "resolved" else "",
    }


class FakeRequest:
    def __init__(self, alerts):
        self._body = {"status": "firing", "alerts": alerts}

    async def json(self):
        return self._body


@pytest.fixture
def webhook(monkeypatch):
    """The endpoint wired to an in-memory incident store."""
    db = FakeSession()
    cluster = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4())
    store = IncidentStore()

    async def noop(*args, **kwargs):
        return None

    async def find_duplicate(_db, _cluster_id, title, window_minutes=None):
        return store.active(title)

    async def create_incident(_db, incident_data, _cluster_id):
        return store.open(incident_data.title)

    async def fake_reconcile(_db, _cluster, alert):
        """What the real reconciler does to the store, and nothing else."""
        title = alerts_module._incident_title(alert)
        store.reconcile_calls.append(title)
        incident = store.active(title)
        if not incident:
            return {"alertname": alert["alertname"], "matched": False,
                    "reason": "no_active_incident"}
        incident.status = models.IncidentStatus.RESOLVED
        return {"alertname": alert["alertname"], "matched": True,
                "incident_id": str(incident.id), "reason": "alert_cleared"}

    async def enqueue(**kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crud, "update_cluster_heartbeat", noop)
    monkeypatch.setattr(crud, "lock_incident_dedup", noop)
    monkeypatch.setattr(crud, "find_duplicate_incident", find_duplicate)
    monkeypatch.setattr(crud, "create_incident", create_incident)
    monkeypatch.setattr(alerts_module, "_reconcile_resolved_alert", fake_reconcile)
    monkeypatch.setattr(alerts_module, "_record_correlation_shadow", noop)
    monkeypatch.setattr(job_worker, "enqueue_and_kick", enqueue)

    def call(alerts):
        return asyncio.run(
            alerts_module.receive_alertmanager_webhook(FakeRequest(alerts), cluster, db)
        )

    return call, store


def test_a_series_that_cleared_does_not_close_an_alert_still_firing(webhook):
    call, store = webhook
    tracking = store.open(
        "[inventory-service] InventorySlowQueries",
        status=models.IncidentStatus.AWAITING_APPROVAL,
    )

    # Interleaved exactly as Alertmanager sends them: some members of the group
    # have cleared, some have not.
    result = call([
        _alert("firing", query="get_item_item-001"),
        _alert("resolved", query="get_item_item-002"),
        _alert("firing", query="get_item_item-003"),
        _alert("resolved", query="get_item_item-004"),
        _alert("firing", query="get_item_item-005"),
    ])

    assert store.reconcile_calls == []
    assert result["incidents_created"] == 0
    assert result["resolved_reconciled"] == 0
    assert len(store.incidents) == 1
    assert store.incidents[0] is tracking
    assert tracking.status == models.IncidentStatus.AWAITING_APPROVAL
    assert [r["reason"] for r in result["reconciliations"]] == [
        "sibling_series_still_firing"
    ] * 2


def test_the_condition_is_reconciled_once_every_series_has_cleared(webhook):
    """The suppression must not swallow a genuine clear: when no member is
    firing, the first resolved member closes the incident and the rest report
    that there is nothing left to close."""
    call, store = webhook
    tracking = store.open(
        "[inventory-service] InventorySlowQueries",
        status=models.IncidentStatus.REMEDIATION_IN_PROGRESS,
    )

    result = call([_alert("resolved", query=f"q-{i}") for i in range(7)])

    assert len(store.reconcile_calls) == 7
    assert result["resolved_reconciled"] == 1
    assert result["incidents_created"] == 0
    assert tracking.status == models.IncidentStatus.RESOLVED


def test_suppression_is_per_alert_not_per_payload(webhook):
    """A different alert clearing in the same delivery is unrelated to the one
    still firing, and must still be reconciled."""
    call, store = webhook
    slow = store.open(
        "[inventory-service] InventorySlowQueries",
        status=models.IncidentStatus.INVESTIGATING,
    )
    errors = store.open(
        "[inventory-service] InventoryHighErrorRate",
        status=models.IncidentStatus.INVESTIGATING,
    )

    result = call([
        _alert("firing", query="get_item_item-001"),
        _alert("resolved", alertname="InventoryHighErrorRate"),
    ])

    assert store.reconcile_calls == ["[inventory-service] InventoryHighErrorRate"]
    assert result["resolved_reconciled"] == 1
    assert result["incidents_created"] == 0
    assert slow.status == models.IncidentStatus.INVESTIGATING
    assert errors.status == models.IncidentStatus.RESOLVED


def test_a_firing_series_still_opens_an_incident_when_there_is_none(webhook):
    """The suppression only guards an alert the payload itself still reports
    firing; a brand-new condition must still open exactly one incident."""
    call, store = webhook

    result = call([
        _alert("firing", query="get_item_item-001"),
        _alert("resolved", query="get_item_item-002"),
        _alert("firing", query="get_item_item-003"),
    ])

    assert result["incidents_created"] == 1
    assert len(store.incidents) == 1
    assert store.reconcile_calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
