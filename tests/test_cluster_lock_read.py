#!/usr/bin/env python3
"""A lock the console cannot read must not be reported as released.

`redis_state_store.is_cluster_locked` fails open: no Redis, answer False. That
is survivable inside the mutation gateway, which rejects with
`state_unavailable` *before* it consults the lock, so enforcement stays closed.
It is not survivable in a read the console renders, because "released" and "we
could not ask" would be the same pixel -- and the operator reading it is the
one deciding whether the platform is currently allowed to touch production.

So the endpoint reports both, and these tests hold the two apart.
"""

import asyncio
import uuid

import sre_agent.redis_state_store as state_store
from sre_agent.api.v1 import clusters


class _Down:
    """Redis unreachable. Answers False, the way the real store does."""

    def is_available(self):
        return False

    def is_cluster_locked(self, _cluster_id):
        raise AssertionError(
            "an unavailable store must not be asked -- its answer is meaningless"
        )


class _Up:
    def __init__(self, locked):
        self._locked = locked

    def is_available(self):
        return True

    def is_cluster_locked(self, _cluster_id):
        return self._locked


def _read(monkeypatch, store):
    monkeypatch.setattr(state_store, "get_state_store", lambda: store)
    return asyncio.run(
        clusters.get_cluster_lock(
            uuid.uuid4(), user=None, db=None, owned_cluster=None
        )
    )


def test_an_unreachable_store_is_reported_as_unknown(monkeypatch):
    assert _read(monkeypatch, _Down()) == {"locked": False, "state_available": False}


def test_a_reachable_store_reports_the_lock_it_holds(monkeypatch):
    assert _read(monkeypatch, _Up(True)) == {"locked": True, "state_available": True}


def test_a_reachable_store_reports_a_released_lock_as_released(monkeypatch):
    """The pairing that stops "always unknown" from passing as caution."""
    assert _read(monkeypatch, _Up(False)) == {"locked": False, "state_available": True}


def test_a_store_without_an_availability_check_is_trusted(monkeypatch):
    """Not every store implementation has `is_available`.

    The mutation gateway guards the same call with `hasattr` for this reason.
    A store that never claims to be unavailable is taken at its word rather
    than reported as permanently unknown, which would make the banner useless.
    """

    class _Plain:
        def is_cluster_locked(self, _cluster_id):
            return True

    assert _read(monkeypatch, _Plain()) == {"locked": True, "state_available": True}
