#!/usr/bin/env python3
"""The lock that stops one alert from opening several war rooms.

`find_duplicate_incident` → `create_incident` is a check-then-create. Run
sequentially it is airtight; run concurrently (Alertmanager landing the same
group on the API more than once at a time) every lookup returns nothing before
any request commits, and each opens its own incident. One real
`InventorySlowQueries` firing produced three incidents 330 ms apart that way.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend import crud


class FakeSession:
    """Records executed statements; mimics an AsyncSession's bind lookup."""

    def __init__(self, dialect="postgresql"):
        self._dialect = dialect
        self.executed = []

    def get_bind(self):
        if self._dialect is None:
            raise RuntimeError("no bind configured")
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect))

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        return None


def test_the_lock_is_taken_before_the_duplicate_lookup():
    db = FakeSession()
    cluster_id = uuid.uuid4()

    assert asyncio.run(crud.lock_incident_dedup(db, cluster_id, "[svc] Alert")) is True
    assert len(db.executed) == 1
    sql, params = db.executed[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"key": crud.incident_dedup_lock_key(cluster_id, "[svc] Alert")}


def test_the_same_alert_on_the_same_cluster_maps_to_one_key():
    cluster_id = uuid.uuid4()
    assert crud.incident_dedup_lock_key(cluster_id, "[svc] Alert") == (
        crud.incident_dedup_lock_key(str(cluster_id), "[svc] Alert")
    )


def test_different_alerts_and_clusters_do_not_serialize_against_each_other():
    """A shared key would make unrelated alerts queue behind one another."""
    cluster_a, cluster_b = uuid.uuid4(), uuid.uuid4()
    keys = {
        crud.incident_dedup_lock_key(cluster_a, "[svc] SlowQueries"),
        crud.incident_dedup_lock_key(cluster_a, "[svc] HighErrorRate"),
        crud.incident_dedup_lock_key(cluster_b, "[svc] SlowQueries"),
    }
    assert len(keys) == 3


def test_the_key_fits_a_postgres_advisory_lock():
    """pg_advisory_xact_lock takes a signed 64-bit integer."""
    key = crud.incident_dedup_lock_key(uuid.uuid4(), "[svc] Alert")
    assert -(2**63) <= key < 2**63


@pytest.mark.parametrize("dialect", ["sqlite", None])
def test_a_bind_without_advisory_locks_says_so_instead_of_failing(dialect):
    """Non-Postgres keeps today's best-effort dedup — but reports no guarantee,
    so a caller can never claim protection it does not have."""
    db = FakeSession(dialect=dialect)
    assert asyncio.run(crud.lock_incident_dedup(db, uuid.uuid4(), "[svc] Alert")) is False
    assert db.executed == []
