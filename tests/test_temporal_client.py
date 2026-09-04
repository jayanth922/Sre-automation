#!/usr/bin/env python3
"""Unit tests for Temporal client bootstrap (local dev server vs Cloud auth)."""

import pytest

import sre_agent.temporal_client as tc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("TEMPORAL_ENABLED", "TEMPORAL_API_KEY", "TEMPORAL_TLS", "TEMPORAL_HOST"):
        monkeypatch.delenv(k, raising=False)
    tc._CLIENT = None
    yield
    tc._CLIENT = None


def test_api_key_unset_returns_none():
    assert tc.temporal_api_key() is None


def test_api_key_set_returns_value(monkeypatch):
    monkeypatch.setenv("TEMPORAL_API_KEY", "tc-key-123")
    assert tc.temporal_api_key() == "tc-key-123"


def test_tls_defaults_false_for_local_dev_server():
    assert tc.temporal_tls() is False


def test_tls_auto_enabled_when_api_key_present(monkeypatch):
    # Temporal Cloud requires TLS on the wire regardless of auth style, so
    # TEMPORAL_HOST + TEMPORAL_API_KEY alone must be enough to reach it.
    monkeypatch.setenv("TEMPORAL_API_KEY", "tc-key-123")
    assert tc.temporal_tls() is True


def test_explicit_tls_env_overrides_api_key_default(monkeypatch):
    monkeypatch.setenv("TEMPORAL_API_KEY", "tc-key-123")
    monkeypatch.setenv("TEMPORAL_TLS", "false")
    assert tc.temporal_tls() is False

    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.setenv("TEMPORAL_TLS", "true")
    assert tc.temporal_tls() is True


def test_get_client_returns_none_when_disabled():
    assert tc.temporal_enabled() is False
    import asyncio

    assert asyncio.run(tc.get_temporal_client()) is None


@pytest.mark.asyncio
async def test_get_client_passes_api_key_and_tls_to_connect(monkeypatch):
    monkeypatch.setenv("TEMPORAL_ENABLED", "true")
    monkeypatch.setenv("TEMPORAL_HOST", "my-ns.my-acct.tmprl.cloud:7233")
    monkeypatch.setenv("TEMPORAL_API_KEY", "tc-key-123")

    calls = {}

    class FakeClient:
        pass

    async def fake_connect(host, *, namespace, api_key, tls):
        calls["host"] = host
        calls["namespace"] = namespace
        calls["api_key"] = api_key
        calls["tls"] = tls
        return FakeClient()

    fake_temporalio_client = type(
        "FakeModule", (), {"Client": type("Client", (), {"connect": staticmethod(fake_connect)})}
    )
    monkeypatch.setitem(
        __import__("sys").modules, "temporalio.client", fake_temporalio_client
    )

    client = await tc.get_temporal_client()
    assert isinstance(client, FakeClient)
    assert calls == {
        "host": "my-ns.my-acct.tmprl.cloud:7233",
        "namespace": "default",
        "api_key": "tc-key-123",
        "tls": True,
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
