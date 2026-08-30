"""Unit tests for GitHub App installation-token minting
(sre_agent/multitenant/github_app.py).

Mirrors tests/test_jira_integration.py's style: monkeypatched httpx, no real
network calls, no real GitHub App required.
"""
import time

import pytest

from sre_agent.multitenant import github_app


class FakeResponse:
    def __init__(self, json_data=None, status=200):
        self._json = json_data or {}
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class FakeAsyncClient:
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
        return FakeResponse({"token": "ghs_minted_token"})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeAsyncClient.instances = []
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    yield


# Freshly generated for this test only — never used for anything real.
_TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpQIBAAKCAQEApQn+EFmxrW2JTtB7BgOYGagVB2y4ZBlo+RaUC+eJuqyJU1K7
MbIAfD23W8OIjbvdSSkfoziTWAa5TiaiDTuZUTS3u2tXmM8qKK4mqF1OkpE+bOd5
rKd4+FyDqqvbBbEJQxPmbegsbYJcMN24I8CcDn+vp4w7qemvb54wnbKi+g0si9g7
KPv1uEzgpLklNyteApfhKOKnOHj25SgP+Dy4bdRBlptAVEbGeRiXQ7wGyVY+tq8Y
ux0dwPTCSB3rLb5fTFUgqo0rlrD2g/gkXtDoG3/e2nCLqT3kvT/zvMHZ/321qVeY
xEvRdMNRcWEMQX+wrFXiyrtDHJQXT/eXiFz7SwIDAQABAoIBAALlsoKZ+H8Jabwq
98XqwTxOEZRwSapkMc4Roea1mVrgFYcTcDrWm6CSusnPHHIUhrV2ldoZL6j/cThY
gEbIMZBV4xXUtBR4Ko7NQ9t3y93R0+04gQ/RXtPJV/xiiPVIHtgBHO34AfOoMrMe
6VEjW/n7Ltu7n/6DHjPQ7JyQGsFV+55ReDgR1wZBGmgHqLl2hPDtx2KI2UXSbBQO
/56VVBa2T6jAAo0qh+l3oeUAQ74l+owQ4SiyVqhcZN/s4UQTf5Ztrlerc909TbB6
5wjoKFENb8Y9i3Y6BDHW/Me0MH5jkj3asOAW7trLx2VRDCZvwaGCFrI5oG5d2DOw
vOUnzWECgYEA4rQyobcypk1EyH7AG0tJdx7tYdwZYXI89yQvM36D+2+Z+nImXn2r
h+zTw/lFSgUNbztfFeCCLgg6UXApfQ3D28B2qv2l4NJkyg6yVFkxCIxFBXnoyYmn
TJX8a20VqZSNY/7YLulx7wZB1Wdk/mQJg0YVU0qEfkVKTcYng/jKFCsCgYEAul3N
TzpeJlf6fD/FBo58A0VpO0C39aHPb3WGAWTtqGbUnlp9RUMQC7RPpd5mGyGUMtLG
q9W15I03N7K9F+lLBtg99+DLW/f+iGdYY5b+R6X7I1Kp3m7rKpkmPtb3dyi6G7ky
0XuonEoYaozyDGwc0uOzOFFNpyMafPyH9qhzhWECgYEA1xE2W15tqYDyOPauDvas
elqXvtfMKDr1BUyJjuN+GCF2xTZXmhrEiM2u1GL9TcxfQ1/iw+FZ/ouFr86lPWK6
pRYAPhUlsZRHU7z/hq+aqc5QiHJv2gpB8ZD0h4FUJK2uOOgCdPa4RJb+C5LsJ74F
nEj3YC34ZcYcSI4s3LFAHEMCgYEAuR7yENAOs5HSu9bwVEn2f51UIUpxMSpRDgs0
WHAz7oJukvmZ09IAv0+VilK3JB4fwrhCJnA7pNJtVgNS98yB/UORkocWGb3mdQIK
96oF3Y/PPdAf8lZFfOPx7JvF5vRqoZ0+EH4AB3dGd5iX2qUNoKIT5U5Fj088QYjr
WaUMUyECgYEAvLo3/coh0X3a6Zn20OJKvTEancYy1TZdKM2Cq9YN9D/UMffb3/tZ
VYw6E6vgpAxcGQoqyBWOmBNNmWDc00syVOl/ILJaCRseFdYiX0VP19hIqMiL3C+O
ycHg0EoakOclCXDvh/J3p6r1MB6OAdzZiP2KaXpLHFXv/LMwAtR/RaE=
-----END RSA PRIVATE KEY-----"""


def _configure(monkeypatch, *, app_id="123", key=_TEST_PRIVATE_KEY, slug="sentinel-sre"):
    monkeypatch.setenv("GITHUB_APP_ID", app_id)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", key)
    if slug:
        monkeypatch.setenv("GITHUB_APP_SLUG", slug)


def test_not_configured_without_app_id_or_key(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    assert not github_app.github_app_configured()


def test_configured_when_app_id_and_key_present(monkeypatch):
    _configure(monkeypatch)
    assert github_app.github_app_configured()


def test_build_app_jwt_has_expected_claims(monkeypatch):
    _configure(monkeypatch)
    from jose import jwt as jose_jwt

    now = int(time.time())
    token = github_app.build_app_jwt(now=now)
    payload = jose_jwt.get_unverified_claims(token)
    assert payload["iss"] == "123"
    assert payload["iat"] <= now
    assert payload["exp"] > now


def test_build_app_jwt_requires_configuration(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(github_app.GitHubAppError):
        github_app.build_app_jwt()


def test_install_url_requires_slug(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
    with pytest.raises(github_app.GitHubAppError):
        github_app.install_url("state123")


def test_install_url_includes_state(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "sentinel-sre")
    url = github_app.install_url("state123")
    assert url == "https://github.com/apps/sentinel-sre/installations/new?state=state123"


@pytest.mark.asyncio
async def test_mint_installation_token_posts_and_returns_token(monkeypatch):
    _configure(monkeypatch)
    token = await github_app.mint_installation_token("999")
    assert token == "ghs_minted_token"
    assert len(FakeAsyncClient.instances) == 1
    method, url, kw = FakeAsyncClient.instances[0].calls[0]
    assert method == "POST"
    assert url.endswith("/app/installations/999/access_tokens")
    assert kw["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_mint_installation_token_requires_installation_id(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(github_app.GitHubAppError):
        await github_app.mint_installation_token("")


@pytest.mark.asyncio
async def test_resolve_github_credential_prefers_minted_token(monkeypatch):
    _configure(monkeypatch)
    creds = {"github_app_installation_id": "999", "github_token": "pat-fallback"}
    token = await github_app.resolve_github_credential(creds)
    assert token == "ghs_minted_token"


@pytest.mark.asyncio
async def test_resolve_github_credential_falls_back_without_installation(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    creds = {"github_token": "pat-fallback"}
    token = await github_app.resolve_github_credential(creds)
    assert token == "pat-fallback"
    assert FakeAsyncClient.instances == []


@pytest.mark.asyncio
async def test_resolve_github_credential_falls_back_on_mint_failure(monkeypatch):
    _configure(monkeypatch)

    class BoomClient(FakeAsyncClient):
        async def post(self, url, **kw):
            raise RuntimeError("github outage")

    monkeypatch.setattr("httpx.AsyncClient", BoomClient)
    creds = {"github_app_installation_id": "999", "github_token": "pat-fallback"}
    token = await github_app.resolve_github_credential(creds)
    assert token == "pat-fallback"
