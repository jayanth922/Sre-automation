"""An empty Loki result must never be readable as evidence of silence.

Loki answers a selector naming a label it does not have exactly the way it
answers a service that logged nothing: HTTP 200, zero streams. In trial 6 the
Application Logs specialist issued five queries against `{app="..."}` - a label
this Loki does not index - and concluded "no evidence of a Loki tool failure ...
just count: 0 ... there's nothing in the logs to contradict the Prometheus
specialist". A query that could never have matched was read as a finding.

These tests pin the distinction: a broken selector is reported as broken, and a
sound selector over a quiet window is reported as genuine silence.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "edge_mcp_servers"
    / "mcp_servers"
    / "loki_real"
    / "server.py"
)

_spec = importlib.util.spec_from_file_location("loki_real_server", MODULE_PATH)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


# Measured against the live Loki behind the Meridian cluster. There is no `app`
# label, and `job` carries the scrape config name, not a service name.
LIVE_LABELS = [
    "container",
    "filename",
    "job",
    "level",
    "namespace",
    "pod",
    "service",
    "stream",
]
LIVE_VALUES = {
    "service": [
        "api-gateway",
        "checkout-service",
        "inventory-service",
        "load-generator",
        "payment-service",
    ],
    "job": ["kubernetes-pods"],
    "namespace": ["kube-system", "meridian", "monitoring"],
    "level": ["ERROR", "INFO", "WARNING"],
}

INVENTORY_SELECTOR = '{service="inventory-service",namespace="meridian"}'


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _stream(line="db pool exhausted", level="ERROR"):
    return {
        "stream": {
            "service": "inventory-service",
            "namespace": "meridian",
            "level": level,
        },
        "values": [["1790140800000000000", line]],
    }


def fake_loki(streams_for=None, labels=LIVE_LABELS, values=None):
    """A stand-in Loki. `streams_for` maps a LogQL query to the streams it
    returns; every other query returns zero streams with HTTP 200 - exactly what
    the real Loki does for a selector that cannot match."""
    streams_for = streams_for or {}
    values = LIVE_VALUES if values is None else values

    def _get(url, params=None, timeout=None):
        params = params or {}
        if url == server.LOKI_QUERY_ENDPOINT:
            result = streams_for.get(params.get("query"), [])
            return FakeResponse({"status": "success", "data": {"result": result}})
        if url == server.LOKI_LABELS_ENDPOINT:
            # Loki omits `data` entirely when it knows of no labels.
            body = {"status": "success"}
            if labels:
                body["data"] = labels
            return FakeResponse(body)
        for name, vals in values.items():
            if url == server.LOKI_LABEL_VALUES_ENDPOINT.format(name=name):
                return FakeResponse({"status": "success", "data": vals})
        return FakeResponse({"status": "success"})

    return _get


# --- selector parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "logql,expected",
    [
        ('{service="a"}', '{service="a"}'),
        ('{service="a"} |= "boom"', '{service="a"}'),
        ('{service="a",namespace="b"} |~ "(?i)error" | json', '{service="a",namespace="b"}'),
        # A brace inside a quoted value must not end the selector.
        ('{service="a}b"} |= "x"', '{service="a}b"}'),
        ("rate(5m)", ""),
    ],
)
def test_split_selector(logql, expected):
    assert server._split_selector(logql) == expected


# --- the trial-6 regression -------------------------------------------------


TRIAL6_APP_QUERIES = [
    '{app="inventory-service", namespace="meridian"} |= "db_connection_refused"',
    '{app="inventory-service", namespace="meridian"} |= "db_timeout"',
    '{app="inventory-service", namespace="meridian"} |= "db_pool_exhausted"',
    '{app="inventory-service", namespace="meridian"}',
]


@pytest.mark.parametrize("logql", TRIAL6_APP_QUERIES)
def test_nonexistent_label_is_reported_as_broken_not_silent(logql):
    """The exact queries trial 6 ran. Each must come back labelled invalid."""
    with patch.object(server.requests, "get", fake_loki()):
        payload = json.loads(server.query_logs(logql=logql, limit=100))

    assert payload["count"] == 0
    assert payload["streams_matched"] == 0
    assert payload["selector_valid"] is False
    assert payload["empty_result_reason"] == "unknown_label"
    assert payload["invalid_label"] == "app"
    assert "NOT EVIDENCE" in payload["warning"]
    assert "service" in payload["warning"]
    # Nothing here may read as a finding about the service.
    assert "note" not in payload


def test_existing_label_with_nonexistent_value_is_reported_as_broken():
    """Trial 6's fallback. `job` is a real label; `inventory-service` is not one
    of its values, so the query still cannot match."""
    logql = '{job="inventory-service", namespace="meridian"}'
    with patch.object(server.requests, "get", fake_loki()):
        payload = json.loads(server.query_logs(logql=logql, limit=100))

    assert payload["selector_valid"] is False
    assert payload["empty_result_reason"] == "unknown_label_value"
    assert payload["invalid_label"] == "job"
    assert payload["invalid_value"] == "inventory-service"
    assert "kubernetes-pods" in payload["warning"]
    assert "NOT EVIDENCE" in payload["warning"]


# --- the cases that are genuine evidence ------------------------------------


def test_valid_selector_whose_filters_excluded_everything_is_evidence():
    """get_error_logs' own selector is correct. When its line filter matches
    nothing but the streams are live, that IS evidence of silence."""
    logql = f'{INVENTORY_SELECTOR} |~ "ERROR"'
    with patch.object(
        server.requests,
        "get",
        fake_loki(streams_for={INVENTORY_SELECTOR: [_stream(level="INFO")]}),
    ):
        payload = json.loads(server.query_logs(logql=logql, limit=100))

    assert payload["selector_valid"] is True
    assert payload["empty_result_reason"] == "filters_excluded_all_lines"
    assert "IS genuine evidence" in payload["note"]
    assert "warning" not in payload


def test_valid_selector_over_a_quiet_window_is_evidence():
    with patch.object(server.requests, "get", fake_loki()):
        payload = json.loads(server.query_logs(logql=INVENTORY_SELECTOR, limit=100))

    assert payload["selector_valid"] is True
    assert payload["empty_result_reason"] == "no_lines_in_window"
    assert "IS genuine evidence" in payload["note"]
    assert "warning" not in payload


def test_results_carry_no_diagnosis():
    with patch.object(
        server.requests,
        "get",
        fake_loki(streams_for={INVENTORY_SELECTOR: [_stream()]}),
    ):
        payload = json.loads(server.query_logs(logql=INVENTORY_SELECTOR, limit=100))

    assert payload["count"] == 1
    assert payload["streams_matched"] == 1
    assert "warning" not in payload
    assert "empty_result_reason" not in payload


# --- the probe must not manufacture a verdict it cannot support -------------


def test_unreadable_label_index_refuses_to_call_it_silence():
    with patch.object(server.requests, "get", fake_loki(labels=[])):
        payload = json.loads(server.query_logs(logql=INVENTORY_SELECTOR, limit=100))

    assert payload["selector_valid"] is None
    assert payload["empty_result_reason"] == "label_probe_unavailable"
    assert "Do NOT treat this as evidence" in payload["warning"]


# --- the callers that used to launder it ------------------------------------


def test_get_error_logs_builds_a_service_selector_and_carries_the_verdict():
    with patch.object(server.requests, "get", fake_loki()) as _:
        payload = json.loads(
            server.get_error_logs(app="inventory-service", namespace="meridian")
        )

    assert 'service="inventory-service"' in payload["query"]
    assert '{app=' not in payload["query"]
    assert payload["selector_valid"] is True


def test_analyze_log_patterns_forwards_the_verdict():
    """It rebuilds its own payload from query_logs' logs list, so without
    explicit forwarding a broken selector arrives as a bare total_logs=0."""
    logql = '{app="inventory-service", namespace="meridian"}'
    with patch.object(server.requests, "get", fake_loki()):
        payload = json.loads(
            server.analyze_log_patterns(logql=logql, pattern="not_found|404")
        )

    assert payload["total_logs"] == 0
    assert payload["selector_valid"] is False
    assert payload["invalid_label"] == "app"
    assert "NOT EVIDENCE" in payload["warning"]


# --- the tool schema must not teach the wrong label -------------------------


def test_query_logs_docstring_does_not_advertise_a_nonexistent_label():
    doc = server.query_logs.__doc__
    assert '{app=' not in doc
    assert 'service=' in doc
