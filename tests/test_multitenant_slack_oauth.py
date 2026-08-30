"""Unit tests for Slack OAuth install-flow (sre_agent/multitenant/slack_oauth.py)."""
from types import SimpleNamespace

import pytest

from sre_agent.multitenant import slack_oauth


class FakeResponse:
    def __init__(self, json_data=None):
        self._json = json_data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class FakeAsyncClient:
    instances = []
    response = FakeResponse(
        {"ok": True, "access_token": "xoxb-abc", "team": {"id": "T123", "name": "Acme"}}
    )

    def __init__(self, *a, **kw):
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeAsyncClient.instances = []
    FakeAsyncClient.response = FakeResponse(
        {"ok": True, "access_token": "xoxb-abc", "team": {"id": "T123", "name": "Acme"}}
    )
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    yield


def _configure(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "client-id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "client-secret")


def test_not_configured_without_client_credentials(monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)
    assert not slack_oauth.slack_oauth_configured()


def test_configured_with_client_credentials(monkeypatch):
    _configure(monkeypatch)
    assert slack_oauth.slack_oauth_configured()


def test_generate_state_is_unique_and_url_safe():
    a, b = slack_oauth.generate_state(), slack_oauth.generate_state()
    assert a != b
    assert len(a) > 20


def test_build_install_url_requires_client_id(monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    with pytest.raises(slack_oauth.SlackOAuthError):
        slack_oauth.build_install_url("state123")


def test_build_install_url_includes_state_and_scopes(monkeypatch):
    _configure(monkeypatch)
    url = slack_oauth.build_install_url("state123")
    assert url.startswith(slack_oauth.SLACK_OAUTH_AUTHORIZE_URL)
    assert "state=state123" in url
    assert "client_id=client-id" in url


@pytest.mark.asyncio
async def test_exchange_code_for_token_success(monkeypatch):
    _configure(monkeypatch)
    result = await slack_oauth.exchange_code_for_token("one-time-code")
    assert result == {"bot_token": "xoxb-abc", "team_id": "T123", "team_name": "Acme"}


@pytest.mark.asyncio
async def test_exchange_code_for_token_requires_configuration(monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)
    with pytest.raises(slack_oauth.SlackOAuthError):
        await slack_oauth.exchange_code_for_token("one-time-code")


@pytest.mark.asyncio
async def test_exchange_code_for_token_raises_on_slack_error(monkeypatch):
    _configure(monkeypatch)
    FakeAsyncClient.response = FakeResponse({"ok": False, "error": "invalid_code"})
    with pytest.raises(slack_oauth.SlackOAuthError, match="invalid_code"):
        await slack_oauth.exchange_code_for_token("bad-code")


@pytest.mark.asyncio
async def test_exchange_code_for_token_raises_on_missing_fields(monkeypatch):
    _configure(monkeypatch)
    FakeAsyncClient.response = FakeResponse({"ok": True})
    with pytest.raises(slack_oauth.SlackOAuthError):
        await slack_oauth.exchange_code_for_token("one-time-code")


def test_resolve_slack_bot_token_prefers_org_token(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-static")
    org = SimpleNamespace(slack_bot_token="xoxb-org-specific")
    assert slack_oauth.resolve_slack_bot_token(org) == "xoxb-org-specific"


def test_resolve_slack_bot_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-static")
    org = SimpleNamespace(slack_bot_token=None)
    assert slack_oauth.resolve_slack_bot_token(org) == "xoxb-static"
