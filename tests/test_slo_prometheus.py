"""Unit tests for wiring SLO.current_value to a real Prometheus query
(src/sre_agent/api/v1/slos.py::_query_current_value)."""
import pytest

from sre_agent.api.v1.slos import _burn_rate, _query_current_value

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


# --- The burn rate the response has always promised and never carried -------
# `burn_rate_1h`/`burn_rate_6h` were `None` with a "Populated by Prometheus
# integration" comment, so `budget_consumed_percent` arrived at the dashboard
# with nothing to say whether it was still climbing.


class RecordingClient(FakeAsyncClient):
    """Captures the PromQL actually sent, and answers per-query."""

    def __init__(self, by_query):
        super().__init__(None)
        self._by_query = by_query
        self.queries = []

    async def get(self, url, params=None):
        query = (params or {}).get("query", "")
        self.queries.append(query)
        return FakeResponse(
            self._by_query.get(
                query, {"status": "success", "data": {"result": []}}
            )
        )


def _scalar(value):
    return {
        "status": "success",
        "data": {"result": [{"metric": {}, "value": [1, str(value)]}]},
    }


def _install_recording(monkeypatch, by_query):
    client = RecordingClient(by_query)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: client)
    return client


@pytest.mark.asyncio
async def test_the_burn_rate_is_the_budget_spend_multiple(monkeypatch):
    """99.0% measured against a 99.9% target: 1% errors against a 0.1%
    budget is ten budgets an hour, and that is the number that turns
    "40% consumed" into "page someone"."""
    sli = "sum(rate(http_success[5m])) / sum(rate(http_total[5m])) * 100"
    subquery = f"avg_over_time(({sli})[1h:])"
    client = _install_recording(monkeypatch, {subquery: _scalar(99.0)})

    rate = await _burn_rate(BASE, sli, 0.999, "1h")

    assert rate == pytest.approx(10.0)
    # The window comes from a subquery around the author's own PromQL --
    # rewriting a range inside it would assume a range it may not contain.
    assert client.queries == [subquery]


@pytest.mark.asyncio
async def test_an_slo_that_is_being_met_is_not_burning(monkeypatch):
    sli = "vector(100)"
    _install_recording(monkeypatch, {f"avg_over_time(({sli})[6h:])": _scalar(100.0)})

    assert await _burn_rate(BASE, sli, 0.999, "6h") == 0.0


@pytest.mark.asyncio
async def test_an_unreachable_prometheus_leaves_the_burn_rate_unknown(monkeypatch):
    """Null, not zero. "Not burning" and "we could not measure" are opposite
    instructions to the person reading the dashboard."""
    _install_recording(monkeypatch, {})

    assert await _burn_rate(BASE, "vector(99)", 0.999, "1h") is None


@pytest.mark.asyncio
async def test_a_hundred_percent_target_has_no_budget_to_burn(monkeypatch):
    """Dividing by a zero budget is infinity, which renders as a burn rate
    rather than as the definition error it is."""
    client = _install_recording(monkeypatch, {})

    assert await _burn_rate(BASE, "vector(99)", 1.0, "1h") is None
    # And it costs no query to find that out.
    assert client.queries == []
