#!/usr/bin/env python3
"""The dedup branch of the Alertmanager webhook must survive a whole group.

An Alertmanager group carries every firing series of the rule — seven for
`InventorySlowQueries`, one per `query` label — so after the first delivery a
payload made *entirely* of duplicates is the normal case, not an edge case.
The dedup branch therefore runs several times per request, and whatever it
does to the session has to leave the session usable for the next alert.

Releasing the advisory lock with `rollback()` did not: rollback expires every
loaded object regardless of `expire_on_commit`, so the next iteration's
`cluster.id` tried to refresh itself with lazy sync IO inside async code —
`MissingGreenlet`, HTTP 500, and Alertmanager retrying the group ten times
before dropping it. Had the incident not already existed, nothing would have
opened one.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend import crud, models
from sre_agent.api.v1 import alerts as alerts_module


class ExpiredAttributeError(RuntimeError):
    """Stands in for sqlalchemy.exc.MissingGreenlet on an expired attribute."""


class FakeSession:
    """Mimics the two session behaviours this branch depends on: commit does
    not expire (``expire_on_commit=False`` in backend/database.py), rollback
    always does."""

    def __init__(self):
        self.expired = False
        self.commits = 0
        self.rollbacks = 0
        self.statements = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.expired = True


class FakeCluster:
    def __init__(self, session):
        self._session = session
        self._id = uuid.uuid4()
        self._org_id = uuid.uuid4()

    def _attr(self, value):
        if self._session.expired:
            raise ExpiredAttributeError(
                "greenlet_spawn has not been called; can't call await_only() here"
            )
        return value

    @property
    def id(self):
        return self._attr(self._id)

    @property
    def org_id(self):
        return self._attr(self._org_id)


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _group_of(n: int) -> dict:
    """One Alertmanager group: n series of the same alert, as it really sends."""
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "InventorySlowQueries",
                    "severity": "warning",
                    "service": "inventory-service",
                    "query": f"get_item_item-{i:03d}",
                },
                "annotations": {"summary": "slow", "description": "slow"},
                "startsAt": "2026-09-14T01:16:00Z",
            }
            for i in range(n)
        ],
    }


@pytest.fixture
def deduping_webhook(monkeypatch):
    """The endpoint with a session that dedups every alert."""
    db = FakeSession()
    cluster = FakeCluster(db)
    # INVESTIGATING: an incident actively being worked, so the dedup branch
    # collapses the alert silently. The parked statuses also post a re-fire
    # notice — that path is covered in test_alert_refire_on_parked_incident.py
    # and would add Slack calls to the lock/commit behaviour under test here.
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        title="[checkout-service] CheckoutHighErrorRate",
        status=models.IncidentStatus.INVESTIGATING,
    )

    async def no_heartbeat(*args, **kwargs):
        return None

    async def always_duplicate(*args, **kwargs):
        return existing

    monkeypatch.setattr(crud, "update_cluster_heartbeat", no_heartbeat)
    monkeypatch.setattr(crud, "find_duplicate_incident", always_duplicate)
    return db, cluster


def test_a_group_of_duplicates_does_not_break_on_the_second_alert(deduping_webhook):
    db, cluster = deduping_webhook

    result = asyncio.run(
        alerts_module.receive_alertmanager_webhook(FakeRequest(_group_of(7)), cluster, db)
    )

    assert result["received"] == 7
    assert result["incidents_created"] == 0


def test_the_dedup_branch_never_rolls_the_session_back(deduping_webhook):
    """Rollback is what expired `cluster` mid-loop; commit ends the lock's
    transaction without expiring anything."""
    db, cluster = deduping_webhook

    asyncio.run(
        alerts_module.receive_alertmanager_webhook(FakeRequest(_group_of(3)), cluster, db)
    )

    assert db.rollbacks == 0
    assert db.commits == 3


def test_every_alert_in_the_group_is_locked_before_its_lookup(deduping_webhook):
    """The lock has to be re-taken each iteration — the previous one was
    released with the transaction that held it."""
    db, cluster = deduping_webhook

    asyncio.run(
        alerts_module.receive_alertmanager_webhook(FakeRequest(_group_of(3)), cluster, db)
    )

    locks = [sql for sql, _ in db.statements if "pg_advisory_xact_lock" in sql]
    assert len(locks) == 3
