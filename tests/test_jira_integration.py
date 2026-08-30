"""Unit tests for the Jira ticketing integration (sre_agent/integrations/jira.py).

Per-tenant credentials live on Cluster (mirrors the existing Notion pattern),
so every test builds a fake cluster/incident row and monkeypatches
backend.crud + backend.database rather than hitting a real database or a
real Jira site.
"""
import uuid
from types import SimpleNamespace

import pytest

from sre_agent.integrations import jira

# Patched by full string path (not a captured module reference) because
# tests/test_config_settings.py pops+reimports backend.database mid-suite;
# a reference captured at collection time would then be stale.


def _cluster(**overrides):
    base = dict(
        id=uuid.uuid4(),
        jira_url="https://acme.atlassian.net",
        jira_email="bot@acme.com",
        jira_api_token="secret-token",
        jira_project_key="OPS",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _incident(**overrides):
    base = dict(id=uuid.uuid4(), cluster_id=uuid.uuid4(), jira_issue_key=None)
    base.update(overrides)
    return SimpleNamespace(**base)


class _NullSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch):
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _NullSessionContext())


class FakeResponse:
    def __init__(self, json_data=None):
        self._json = json_data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class FakeAsyncClient:
    """Records every call; args[0]/kwargs available for assertions."""

    instances = []

    def __init__(self, *a, **kw):
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if "/issue" in url and "/comment" not in url and "/transitions" not in url:
            return FakeResponse({"key": "OPS-42"})
        return FakeResponse({})

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return FakeResponse(
            {"transitions": [{"id": "31", "name": "In Progress"}, {"id": "41", "name": "Done"}]}
        )


@pytest.fixture(autouse=True)
def _reset_fake_client():
    FakeAsyncClient.instances = []
    yield
    FakeAsyncClient.instances = []


def _patch_httpx(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)


# ── pure logic ───────────────────────────────────────────────────────────────
def test_jira_configured_requires_all_four_fields():
    assert jira.jira_configured(_cluster())
    assert not jira.jira_configured(_cluster(jira_project_key=None))
    assert not jira.jira_configured(None)


def test_severity_to_priority_known_and_unknown():
    assert jira.SEVERITY_TO_PRIORITY["critical"] == "Highest"
    assert jira.SEVERITY_TO_PRIORITY.get("nonsense", "Medium") == "Medium"


def test_status_transition_default_mapping():
    assert jira._status_transition_name("resolved") == "Done"
    assert jira._status_transition_name("investigating") == "In Progress"
    assert jira._status_transition_name("totally-unknown") is None


def test_status_transition_env_override(monkeypatch):
    monkeypatch.setenv("JIRA_TRANSITION_RESOLVED", "Closed")
    assert jira._status_transition_name("resolved") == "Closed"


# ── maybe_create_jira_issue ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_issue_noop_when_cluster_not_configured(monkeypatch):
    _patch_session(monkeypatch)
    _patch_httpx(monkeypatch)

    async def fake_get_cluster(db, cid):
        return _cluster(jira_url=None)

    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster)

    await jira.maybe_create_jira_issue(str(uuid.uuid4()), str(uuid.uuid4()), "PodCrashLooping", "summary", "high")

    assert FakeAsyncClient.instances == []  # no network call made


@pytest.mark.asyncio
async def test_create_issue_posts_and_persists_key(monkeypatch):
    _patch_session(monkeypatch)
    _patch_httpx(monkeypatch)
    cluster = _cluster()
    incident_id = uuid.uuid4()

    set_key_calls = []

    async def fake_set_key(db, iid, key):
        set_key_calls.append((iid, key))

    monkeypatch.setattr("backend.crud.set_incident_jira_key", fake_set_key)

    async def fake_get_cluster(db, cid):
        return cluster

    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster)

    await jira.maybe_create_jira_issue(str(incident_id), str(cluster.id), "PodCrashLooping", "it broke", "critical")

    assert len(FakeAsyncClient.instances) == 1
    method, url, kw = FakeAsyncClient.instances[0].calls[0]
    assert method == "POST" and url.endswith("/rest/api/3/issue")
    fields = kw["json"]["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["priority"] == {"name": "Highest"}
    assert kw["auth"] == (cluster.jira_email, cluster.jira_api_token)
    assert set_key_calls == [(incident_id, "OPS-42")]


# ── transition_jira_issue ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transition_noop_without_linked_issue(monkeypatch):
    _patch_session(monkeypatch)
    _patch_httpx(monkeypatch)
    cluster = _cluster()
    incident = _incident(jira_issue_key=None)

    async def fake_get_cluster(db, cid):
        return cluster

    async def fake_get_incident(db, iid):
        return incident

    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster)
    monkeypatch.setattr("backend.crud.get_incident_by_id", fake_get_incident)

    await jira.transition_jira_issue(str(incident.id), str(cluster.id), "resolved")

    assert FakeAsyncClient.instances == []


@pytest.mark.asyncio
async def test_transition_moves_status_and_comments(monkeypatch):
    _patch_session(monkeypatch)
    _patch_httpx(monkeypatch)
    cluster = _cluster()
    incident = _incident(jira_issue_key="OPS-42")

    async def fake_get_cluster(db, cid):
        return cluster

    async def fake_get_incident(db, iid):
        return incident

    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster)
    monkeypatch.setattr("backend.crud.get_incident_by_id", fake_get_incident)

    await jira.transition_jira_issue(str(incident.id), str(cluster.id), "resolved", comment="postmortem body")

    client = FakeAsyncClient.instances[0]
    methods_urls = [(m, u) for m, u, _ in client.calls]
    assert ("GET", "https://acme.atlassian.net/rest/api/3/issue/OPS-42/transitions") in methods_urls
    assert ("POST", "https://acme.atlassian.net/rest/api/3/issue/OPS-42/transitions") in methods_urls
    assert ("POST", "https://acme.atlassian.net/rest/api/3/issue/OPS-42/comment") in methods_urls
    # transition call used the matched "Done" id, not the raw name
    transition_call = next(kw for m, u, kw in client.calls if u.endswith("/transitions") and m == "POST")
    assert transition_call["json"] == {"transition": {"id": "41"}}


@pytest.mark.asyncio
async def test_transition_skips_unknown_transition_name(monkeypatch):
    _patch_session(monkeypatch)
    _patch_httpx(monkeypatch)
    cluster = _cluster()
    incident = _incident(jira_issue_key="OPS-42")

    async def fake_get_cluster(db, cid):
        return cluster

    async def fake_get_incident(db, iid):
        return incident

    monkeypatch.setattr("backend.crud.get_cluster_by_id", fake_get_cluster)
    monkeypatch.setattr("backend.crud.get_incident_by_id", fake_get_incident)
    monkeypatch.setenv("JIRA_TRANSITION_RESOLVED", "No Such Transition")

    await jira.transition_jira_issue(str(incident.id), str(cluster.id), "resolved")

    client = FakeAsyncClient.instances[0]
    posts = [(m, u) for m, u, _ in client.calls if m == "POST" and u.endswith("/transitions")]
    assert posts == []  # never posted a transition it couldn't match
