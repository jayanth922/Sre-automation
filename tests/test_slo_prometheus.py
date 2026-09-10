"""Unit tests for wiring SLO.current_value to a real Prometheus query
(sre_agent/api/v1/slos.py::_query_current_value)."""
import pytest

from sre_agent.api.v1.slos import _query_current_value

BASE = "http://prom.example:9090"


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        return self._result


def _install(monkeypatch, result):
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: FakeAsyncClient(result))


@pytest.mark.asyncio
async def test_returns_scalar_on_success(monkeypatch):
    _install(monkeypatch, FakeResponse({
        "status": "success",
        "data": {"result": [{"metric": {}, "value": [1234567890, "99.95"]}]},
    }))
    value = await _query_current_value(BASE, "vector(99.95)")
    assert value == 99.95


@pytest.mark.asyncio
async def test_returns_none_on_empty_result(monkeypatch):
    _install(monkeypatch, FakeResponse({"status": "success", "data": {"result": []}}))
    assert await _query_current_value(BASE, "up{job=\"missing\"}") is None


@pytest.mark.asyncio
async def test_returns_none_on_error_status(monkeypatch):
    _install(monkeypatch, FakeResponse({"status": "error", "error": "bad_data"}))
    assert await _query_current_value(BASE, "not a valid promql (") is None


@pytest.mark.asyncio
async def test_returns_none_on_unreachable_prometheus(monkeypatch):
    class BrokenClient(FakeAsyncClient):
        async def get(self, url, params=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: BrokenClient(None))
    assert await _query_current_value(BASE, "vector(1)") is None
