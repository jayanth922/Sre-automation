#!/usr/bin/env python3
"""Autonomous remediation was unreachable in production, and the tests passed.

`compute_urgency_score` returns `(None, True)` unless at least one of
`slo_burn_rate`, `saturation` or `error_rate_slope` is present, and every one
of those three had no producer in the running system:

  * nothing anywhere computed `slo_burn_rate` or `error_rate_slope` outside
    test fixtures, and
  * the one real `saturation` producer — the Prometheus MCP's golden-signals
    tool — reported it as `{"query": …, "value": …}`, and `_walk_metrics`
    let that wrapper dict claim the `saturation` slot and then skipped the
    number nested inside it.

So urgency was always unknown, `classify_severity` always escalated to
UNKNOWN, `is_low_severity` was always False, and the policy gate could never
grant autonomy — for any incident, at any real severity. Confirmed live on
incidents `5cc643c5` and `281b8110` (2026-09-17), the second of which kept its
fault injected for the whole run and still classified UNKNOWN.

The existing severity tests all passed throughout, because each one
constructed `IncidentSignals(slo_burn_rate=…, saturation=…)` by hand — they
tested the scoring maths, and nothing tested that the pipeline could ever
supply its inputs. These tests do that: the walker must read the real MCP
payload shape, and the platform must be able to measure the three urgency
inputs itself rather than hoping the model ran the right query.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict

import httpx
import pytest

from sre_agent import graph_builder
from sre_agent import metrics_profile as mp
from sre_agent import severity_telemetry as st
from sre_agent.act_phase import _walk_metrics, extract_incident_signals
from sre_agent.agent_state import AlertContext
from sre_agent.severity_engine import Severity, classify_severity, is_low_severity

PROFILE = {
    "service_label": "service",
    "request_metric": "http_requests_total",
    "status_label": "status",
    "error_regex": "5..",
    "latency_histogram": "http_request_duration_seconds",
    "cpu_query": "avg(rate(container_cpu_usage_seconds_total[5m])) * 100",
    "mem_query": "sum(container_memory_usage_bytes)",
    "saturation_query": "avg(cpu_used) / avg(cpu_limit)",
    "slo_target": "0.99",
}


# ── The extraction bug ───────────────────────────────────────────────────────
def _signal(query, series):
    """One entry of `get_golden_signals`, in the shape it actually returns."""
    return {
        "query": query,
        "value": {
            "result": series,
            "series_returned": len(series),
            "series_total": len(series),
            "series_truncated": False,
        },
    }


def _series(value, **labels):
    return [{"metric": labels, "value": [1789000000.0, str(value)]}]


def test_walker_reads_the_real_mcp_saturation_shape():
    """The exact payload `get_golden_signals` returns, which used to yield None."""
    payload = {
        "saturation": _signal("avg(container_cpu_usage_seconds_total)", _series(0.42)),
        "error_rate": _signal("sum(rate(http_requests_total))", _series(3.5)),
    }
    found = _walk_metrics(payload)
    assert found["saturation"][0] == "0.42"
    assert found["error_rate"][0] == "3.5"


def test_walker_refuses_to_pick_one_of_several_series():
    """Several series are several measurements; choosing one would be a guess."""
    many = _series(0.42, pod="a") + _series(0.99, pod="b")
    assert _walk_metrics({"saturation": _signal("avg(cpu)", many)}) == {}


def test_walker_reports_nothing_for_an_empty_or_failed_query():
    assert _walk_metrics({"saturation": _signal("avg(cpu)", [])}) == {}
    assert _walk_metrics({"saturation": {"query": "avg(cpu)", "error": "boom"}}) == {}


def test_walker_still_prefers_the_shallowest_real_value():
    """Fixing slot-poisoning must not change precedence for usable values."""
    payload = {"saturation": 0.1, "nested": {"saturation": 0.9}}
    assert _walk_metrics(payload)["saturation"][0] == 0.1


# ── Measured zero is not missing data ────────────────────────────────────────
@pytest.mark.parametrize(
    "errors,total,reached,expected",
    [
        # Traffic flowing, no 5xx series exists at all: Prometheus returns
        # nothing for the numerator, but this is a counted zero, not silence.
        (None, 2.53, True, 0.0),
        (0.0, 2.53, True, 0.0),
        (0.25, 2.5, True, 0.1),
        # No traffic: the ratio is undefined. Reporting 0.0 would describe a
        # service that is receiving nothing as perfectly healthy.
        (None, 0.0, True, None),
        (None, None, True, None),
        # Query never completed: no evidence of anything.
        (None, 2.53, False, None),
    ],
)
def test_error_ratio_separates_measured_zero_from_no_measurement(
    errors, total, reached, expected
):
    assert st.error_ratio(errors, total, reached=reached) == expected


# ── Profile parsing ──────────────────────────────────────────────────────────
def test_severity_fields_are_optional():
    cfg = {k: v for k, v in PROFILE.items() if k not in mp.SEVERITY_KEYS}
    resolved = mp.resolve(json.dumps(cfg), "prod")
    assert mp.slo_target(resolved) is None
    assert mp.q_sev_saturation(resolved) is None


@pytest.mark.parametrize("bad", ["ninety-nine", "99", "1.0", "0", "-0.5"])
def test_malformed_slo_target_is_surfaced_not_ignored(bad):
    """Silently dropping it would leave burn rate unmeasured with no reason why."""
    cfg = {**PROFILE, "slo_target": bad}
    with pytest.raises(mp.MetricsProfileMalformed):
        mp.resolve(json.dumps(cfg), "prod")


def test_saturation_query_is_scoped_to_the_alerting_service():
    scoped = {**PROFILE, "saturation_query": 'avg(cpu{job="$service"}) / avg(lim)'}
    c = mp.resolve(json.dumps(scoped), "prod")
    assert mp.q_sev_saturation(c, "checkout") == 'avg(cpu{job="checkout"}) / avg(lim)'
    # Nothing to scope it to: sending `$service` through would be a PromQL
    # parse error and blanking it would silently widen the match.
    assert mp.q_sev_saturation(c, "") is None
    # A cluster-wide expression is still honoured verbatim.
    plain = mp.resolve(json.dumps(PROFILE), "prod")
    assert mp.q_sev_saturation(plain, "checkout") == PROFILE["saturation_query"]


def test_severity_queries_scope_to_one_service_and_keep_numerator_apart():
    c = mp.resolve(json.dumps(PROFILE), "prod")
    total = mp.q_sev_requests(c, "checkout", "5m")
    errors = mp.q_sev_errors(c, "checkout", "5m")
    prev = mp.q_sev_errors(c, "checkout", "5m", offset="5m")
    assert 'service="checkout"' in total and 'namespace="prod"' in total
    assert 'status=~"5.."' in errors and 'status=~"5.."' not in total
    assert prev.endswith("offset 5m))")


# ── Measurement against a stand-in Prometheus ────────────────────────────────
def _prometheus(values):
    """A Prometheus that answers from `values`: query substring → scalar/None."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        seen.append(query)
        for needle, value in values.items():
            if needle in query:
                if value is None:
                    break
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"result": [{"value": [0, str(value)]}]},
                    },
                )
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    return handler, seen


@pytest.fixture
def prometheus(monkeypatch):
    # `st.httpx` is the httpx module itself, so bind the real class before
    # patching or the replacement calls itself.
    real = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            st.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler)),
        )

    def values(mapping):
        handler, seen = _prometheus(mapping)
        install(handler)
        return seen

    values.install = install
    return values


@pytest.mark.asyncio
async def test_measures_all_three_urgency_inputs(prometheus):
    prometheus(
        {
            'status=~"5.."}[5m] offset 5m': 0.02,
            'status=~"5.."}[5m]': 0.10,
            "[5m] offset 5m": 2.0,
            "[5m]": 2.0,
            "cpu_used": 0.65,
        }
    )
    out = await st.measure_severity_signals(
        prometheus_url="http://prom:9090",
        metrics_config=json.dumps(PROFILE),
        service="checkout",
        namespace="prod",
    )
    m = out["metrics"]
    assert m["error_rate"] == pytest.approx(0.05)
    assert m["slo_burn_rate"] == pytest.approx(5.0)  # 0.05 / (1 - 0.99)
    assert m["slo_breached"] is True
    assert m["saturation"] == pytest.approx(0.65)
    # (0.05 - 0.01) over a 5-minute step.
    assert m["error_rate_slope"] == pytest.approx(0.008)
    assert set(out["sources"]) == set(m)


@pytest.mark.asyncio
async def test_healthy_service_measures_a_real_zero(prometheus):
    """Traffic with no failures must classify, not fall through to UNKNOWN."""
    prometheus({'status=~"5.."': None, "[5m]": 2.53, "cpu_used": 0.2})
    out = await st.measure_severity_signals(
        prometheus_url="http://prom:9090",
        metrics_config=json.dumps(PROFILE),
        service="checkout",
        namespace="prod",
    )
    m = out["metrics"]
    assert m["error_rate"] == 0.0
    assert m["slo_burn_rate"] == 0.0
    assert m["slo_breached"] is False
    assert m["error_rate_slope"] == 0.0


@pytest.mark.asyncio
async def test_unconfigured_cluster_measures_nothing(prometheus):
    """The fail-safe: no profile means no numbers, not guessed ones."""
    prometheus({"": 1.0})
    assert (
        await st.measure_severity_signals(
            prometheus_url="http://prom:9090",
            metrics_config=None,
            service="checkout",
        )
        == {}
    )
    assert (
        await st.measure_severity_signals(
            prometheus_url=None,
            metrics_config=json.dumps(PROFILE),
            service="checkout",
        )
        == {}
    )


@pytest.mark.asyncio
async def test_saturation_alone_when_the_cluster_sets_no_slo(prometheus):
    cfg = {k: v for k, v in PROFILE.items() if k != "slo_target"}
    prometheus({"cpu_used": 0.7, "[5m]": 2.0})
    out = await st.measure_severity_signals(
        prometheus_url="http://prom:9090",
        metrics_config=json.dumps(cfg),
        service="checkout",
        namespace="prod",
    )
    assert out["metrics"]["saturation"] == pytest.approx(0.7)
    assert "slo_burn_rate" not in out["metrics"]


@pytest.mark.asyncio
async def test_prometheus_failure_is_absence_not_a_zero(prometheus):
    def explode(request):
        raise httpx.ConnectError("no route to host")

    prometheus.install(explode)
    out = await st.measure_severity_signals(
        prometheus_url="http://prom:9090",
        metrics_config=json.dumps(PROFILE),
        service="checkout",
        namespace="prod",
    )
    assert out == {}


# ── The whole point: the gate can now see ────────────────────────────────────
def _state(telemetry=None):
    state = {
        "alert_context": {
            "labels": {"service": "checkout", "severity": "warning"},
            "annotations": {},
        },
        "agent_results": {"metrics_agent": "Checked the dashboards, looks hot."},
        "metadata": {},
    }
    if telemetry:
        state["metadata"]["severity_telemetry"] = telemetry
    return state


def test_without_platform_telemetry_severity_is_unknown():
    """The production defect, pinned so it cannot come back quietly."""
    assessment = classify_severity(extract_incident_signals(_state()))
    assert assessment.severity is Severity.UNKNOWN
    assert not is_low_severity(assessment.severity)


def test_platform_telemetry_makes_urgency_measurable():
    signals = extract_incident_signals(
        _state(
            {
                "metrics": {
                    "error_rate": 0.001,
                    "slo_burn_rate": 0.1,
                    "slo_breached": False,
                    "error_rate_slope": -0.002,
                    "saturation": 0.2,
                },
                "sources": {"error_rate": "sum(rate(http_requests_total{…}))"},
                "service": "checkout",
            }
        )
    )
    assert signals.slo_burn_rate == 0.1
    assert signals.saturation == 0.2
    assert signals.error_rate_slope == -0.002
    assert classify_severity(signals).severity is not Severity.UNKNOWN


def test_query_strings_in_sources_never_become_measurements():
    """`sources` sits beside `metrics`; a PromQL string is not a number."""
    signals = extract_incident_signals(
        _state(
            {
                "metrics": {"saturation": 0.2},
                "sources": {
                    "saturation": "avg(cpu)/avg(limit)",
                    "error_rate": "sum(rate(http_requests_total))",
                    "slo_breached": "error_rate > 0.01",
                },
            }
        )
    )
    assert signals.saturation == 0.2
    assert signals.error_rate is None
    assert signals.slo_breached is None


def test_platform_measurement_outranks_a_model_reported_one():
    """Both are Prometheus numbers; only one was scoped by the platform."""
    state = _state({"metrics": {"error_rate": 0.02}})
    state["agent_results"] = {"metrics_agent": {"error_rate": 0.9}}
    assert extract_incident_signals(state).error_rate == 0.02


# ── The node's own inputs ────────────────────────────────────────────────────
# Everything above proves the measurement is right *given* a service name. The
# node also has to get one, and that is where it first failed in production:
# at runtime `alert_context` is an `AlertContext` model, not the dict every
# test here builds, so a dict-only read found no labels, passed an empty
# service, and turned the node into a silent no-op while the whole suite
# stayed green. These two tests run the node itself, once per shape.
@pytest.mark.parametrize(
    "alert",
    [
        pytest.param(
            AlertContext(
                alert_name="InventorySlowQueries",
                severity="warning",
                labels={"service": "inventory-service", "namespace": "meridian"},
            ),
            id="AlertContext model (production)",
        ),
        pytest.param(
            {"labels": {"service": "inventory-service", "namespace": "meridian"}},
            id="plain dict (tests, resumed checkpoints)",
        ),
    ],
)
@pytest.mark.asyncio
async def test_node_reads_the_service_from_either_alert_shape(alert, monkeypatch):
    seen: Dict[str, Any] = {}

    async def fake_measure(*, cluster_id, service):
        seen["cluster_id"], seen["service"] = cluster_id, service
        return {"metrics": {"saturation": 0.3}, "sources": {}, "service": service}

    monkeypatch.setattr(st, "measure_for_incident", fake_measure)

    out = await graph_builder._severity_telemetry_node(
        {"alert_context": alert, "metadata": {"existing": 1}},
        SimpleNamespace(cluster_id="bcbd9577-3195-45e4-b840-da592647459c"),
    )

    assert seen["service"] == "inventory-service"
    assert seen["cluster_id"] == "bcbd9577-3195-45e4-b840-da592647459c"
    assert out["metadata"]["severity_telemetry"]["metrics"]["saturation"] == 0.3
    # The node merges into metadata rather than replacing it.
    assert out["metadata"]["existing"] == 1


@pytest.mark.asyncio
async def test_node_measuring_nothing_leaves_state_untouched(monkeypatch):
    """An unmeasurable incident must degrade, not fail the investigation."""

    async def fake_measure(*, cluster_id, service):
        raise RuntimeError("Prometheus is down")

    monkeypatch.setattr(st, "measure_for_incident", fake_measure)

    out = await graph_builder._severity_telemetry_node(
        {"alert_context": {"labels": {}}, "metadata": {}},
        SimpleNamespace(cluster_id="bcbd9577-3195-45e4-b840-da592647459c"),
    )
    assert out == {}
