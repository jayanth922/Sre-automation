"""Unit tests for Prometheus discovery heuristics (sre_agent/metrics_discovery.py)."""
import pytest

from sre_agent.metrics_discovery import MetricsDiscoveryError, discover_metrics_profile

BASE = "http://prom.example:9090"


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, routes, *a, **kw):
        self.routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        path = url[len(BASE):]
        if path == "/api/v1/label/__name__/values":
            return FakeResponse(self.routes.get("__name__", {"status": "success", "data": []}))
        if path == "/api/v1/series":
            metric = (params or {}).get("match[]")
            return FakeResponse(self.routes.get(("series", metric), {"status": "success", "data": []}))
        if path.startswith("/api/v1/label/") and path.endswith("/values"):
            label = path[len("/api/v1/label/"):-len("/values")]
            return FakeResponse(self.routes.get(("values", label), {"status": "success", "data": []}))
        return FakeResponse({"status": "error"})


def _install(monkeypatch, routes):
    def factory(*a, **kw):
        return FakeAsyncClient(routes, *a, **kw)

    monkeypatch.setattr("httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_requires_prometheus_url():
    with pytest.raises(MetricsDiscoveryError, match="required"):
        await discover_metrics_profile("")


@pytest.mark.asyncio
async def test_unreachable_prometheus_raises(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: FakeAsyncClient({}, *a, **kw))

    class BrokenClient(FakeAsyncClient):
        async def get(self, url, params=None):
            raise RuntimeError("boom")

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: BrokenClient({}, *a, **kw))
    with pytest.raises(MetricsDiscoveryError, match="Could not reach"):
        await discover_metrics_profile(BASE)


@pytest.mark.asyncio
async def test_full_discovery_happy_path(monkeypatch):
    routes = {
        "__name__": {
            "status": "success",
            "data": [
                "http_requests_total",
                "http_request_duration_seconds_bucket",
                "container_cpu_usage_seconds_total",
                "container_memory_usage_bytes",
                "unrelated_metric",
            ],
        },
        ("series", "http_requests_total"): {
            "status": "success",
            "data": [
                {"__name__": "http_requests_total", "service": "checkout", "status_code": "200", "job": "checkout"},
                {"__name__": "http_requests_total", "service": "checkout", "status_code": "500", "job": "checkout"},
            ],
        },
        ("values", "status_code"): {"status": "success", "data": ["200", "404", "500"]},
    }
    _install(monkeypatch, routes)

    result = await discover_metrics_profile(BASE, namespace="prod")

    assert result["request_metric"]["candidates"] == ["http_requests_total"]
    assert result["latency_histogram"]["candidates"] == ["http_request_duration_seconds"]
    assert "service" in result["service_label"]["candidates"]
    assert result["status_label"]["candidates"][0] == "status_code"
    assert result["error_regex"]["suggestion"] == "5.."
    assert "container_cpu_usage_seconds_total" in result["cpu_query"]["suggestion"]
    assert 'namespace="prod"' in result["cpu_query"]["suggestion"]
    assert "container_memory_usage_bytes" in result["mem_query"]["suggestion"]


@pytest.mark.asyncio
async def test_no_matching_metrics_degrades_gracefully(monkeypatch):
    routes = {"__name__": {"status": "success", "data": ["unrelated_metric"]}}
    _install(monkeypatch, routes)

    result = await discover_metrics_profile(BASE)

    assert result["request_metric"]["candidates"] == []
    assert result["request_metric"]["note"]
    assert result["service_label"]["candidates"] == []
    assert result["error_regex"]["suggestion"] is None
    assert result["cpu_query"]["suggestion"] is None
    assert result["mem_query"]["suggestion"] is None


@pytest.mark.asyncio
async def test_non_http_status_values_skip_error_regex_suggestion(monkeypatch):
    routes = {
        "__name__": {
            "status": "success",
            "data": ["http_requests_total"],
        },
        ("series", "http_requests_total"): {
            "status": "success",
            "data": [{"__name__": "http_requests_total", "status_code": "ok"}],
        },
        ("values", "status_code"): {"status": "success", "data": ["ok", "error"]},
    }
    _install(monkeypatch, routes)

    result = await discover_metrics_profile(BASE)

    assert result["error_regex"]["suggestion"] is None
    assert result["error_regex"]["note"]
