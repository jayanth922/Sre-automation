#!/usr/bin/env python3
"""Unit tests for the restart-safe WarRoomRegistry singleton (rehydration)."""

from types import SimpleNamespace

import pytest

from sre_agent import war_room_service
from sre_agent.war_room import ThreadRef


@pytest.fixture(autouse=True)
def _reset_registry_singleton(monkeypatch):
    """Each test starts from a clean, un-hydrated singleton."""
    monkeypatch.setattr(war_room_service, "_registry", None)
    monkeypatch.setattr(war_room_service, "_registry_hydrated", False)
    yield


def _patch_db(monkeypatch, get_incidents_fn):
    """Patch via dotted string targets, not imported module objects: some
    other test module (test_config_settings.py) pops and reimports
    backend.database mid-suite, which would leave an object-based patch
    pointed at a stale module no one uses anymore. String targets always
    resolve whatever is currently in sys.modules."""
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr("backend.crud.get_incidents_with_open_slack_threads", get_incidents_fn)


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_get_registry_rehydrates_from_open_slack_threads(monkeypatch):
    incidents = [
        SimpleNamespace(id="inc-1", slack_channel="C1", slack_thread_ts="111.111"),
        SimpleNamespace(id="inc-2", slack_channel="C2", slack_thread_ts="222.222"),
    ]

    async def fake_get_incidents_with_open_slack_threads(db):
        return incidents

    _patch_db(monkeypatch, fake_get_incidents_with_open_slack_threads)

    registry = await war_room_service._get_registry()

    assert registry.incident_for(ThreadRef("C1", "111.111")) == "inc-1"
    assert registry.incident_for(ThreadRef("C2", "222.222")) == "inc-2"


@pytest.mark.asyncio
async def test_get_registry_hydrates_only_once(monkeypatch):
    calls = []

    async def fake_get_incidents_with_open_slack_threads(db):
        calls.append(1)
        return []

    _patch_db(monkeypatch, fake_get_incidents_with_open_slack_threads)

    await war_room_service._get_registry()
    await war_room_service._get_registry()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_registry_hydration_failure_is_non_fatal(monkeypatch):
    async def fake_get_incidents_with_open_slack_threads(db):
        raise RuntimeError("db unreachable")

    _patch_db(monkeypatch, fake_get_incidents_with_open_slack_threads)

    registry = await war_room_service._get_registry()

    assert registry.thread_for("inc-1") is None


@pytest.mark.asyncio
async def test_maybe_open_war_room_uses_org_token_not_env(monkeypatch):
    """No process-wide SLACK_BOT_TOKEN fallback: resolves the incident's own
    cluster -> organization and uses that org's Slack token, or no-ops."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-operator-static")

    cluster = SimpleNamespace(org_id="org-1")
    org = SimpleNamespace(slack_bot_token="xoxb-org-1-real")

    async def fake_get_cluster_by_id(db, cluster_id):
        return cluster

    async def fake_get_org_by_id(db, org_id):
        assert org_id == "org-1"
        return org

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr("backend.crud.get_org_by_id", fake_get_org_by_id)

    captured = {}

    class _FakeAsyncApp:
        def __init__(self, token):
            captured["token"] = token
            # No .client mock: chat_postMessage will raise AttributeError,
            # caught by the function's own non-fatal except — fine, the
            # token capture above already happened by then.

    monkeypatch.setattr("slack_bolt.async_app.AsyncApp", _FakeAsyncApp)

    # cluster_id must parse as a UUID: maybe_open_war_room does
    # uuid.UUID(str(cluster_id)) before the cluster lookup.
    await war_room_service.maybe_open_war_room(
        "inc-1", "00000000-0000-0000-0000-000000000001", "summary"
    )

    assert captured["token"] == "xoxb-org-1-real"


@pytest.mark.asyncio
async def test_maybe_open_war_room_noops_without_org_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cluster = SimpleNamespace(org_id="org-1")
    org = SimpleNamespace(slack_bot_token=None)

    async def fake_get_cluster_by_id(db, cluster_id):
        return cluster

    async def fake_get_org_by_id(db, org_id):
        return org

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster_by_id)
    monkeypatch.setattr("backend.crud.get_org_by_id", fake_get_org_by_id)

    # Must return cleanly without raising and without posting to Slack.
    await war_room_service.maybe_open_war_room(
        "inc-1", "00000000-0000-0000-0000-000000000001", "summary"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
