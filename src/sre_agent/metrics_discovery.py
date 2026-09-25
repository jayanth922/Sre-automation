"""Prometheus-backed discovery of candidate values for a cluster's
observability profile (sre_agent.metrics_profile).

This module never decides a cluster's profile — it only proposes candidates
for the admin to review and pick in Settings -> AI & metrics. Nothing here
is auto-applied or auto-saved; see metrics_profile.py's own fail-closed
philosophy, which this module must not undermine.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

_TIMEOUT = 10.0
_MAX_CANDIDATES = 10


class MetricsDiscoveryError(Exception):
    """Raised when Prometheus itself cannot be reached or returns garbage.

    A failure to resolve any single field (e.g. no matching metric found)
    is NOT this error — it degrades that field to an empty candidate list
    with an explanatory note instead of failing the whole discovery.
    """

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


async def _get_json(client: httpx.AsyncClient, base: str, path: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(f"{base}{path}", params=params or {})
        data = resp.json()
    except Exception:
        return None
    if data.get("status") != "success":
        return None
    return data


def _rank(names: List[str], keywords: List[str]) -> List[str]:
    """Rank names by the earliest-matching keyword, then alphabetically."""

    def score(name: str) -> tuple:
        lowered = name.lower()
        for idx, kw in enumerate(keywords):
            if kw in lowered:
                return (idx, name)
        return (len(keywords), name)

    matched = [n for n in names if score(n)[0] < len(keywords)]
    matched.sort(key=score)
    return matched[:_MAX_CANDIDATES]


def _rank_request_metrics(metric_names: List[str]) -> List[str]:
    candidates = [
        n
        for n in metric_names
        if "request" in n.lower() and (n.endswith("_total") or n.endswith("_count"))
    ]
    candidates.sort(key=lambda n: (0 if "http" in n.lower() else 1, n))
    return candidates[:_MAX_CANDIDATES]


def _rank_latency_histograms(metric_names: List[str]) -> List[str]:
    keywords = ("duration", "latency", "histogram")
    candidates = []
    for n in metric_names:
        if not n.endswith("_bucket"):
            continue
        base_name = n[: -len("_bucket")]
        if any(k in base_name.lower() for k in keywords):
            candidates.append(base_name)
    candidates.sort()
    return candidates[:_MAX_CANDIDATES]


async def _series_labels(client: httpx.AsyncClient, base: str, metric: str) -> List[str]:
    data = await _get_json(client, base, "/api/v1/series", {"match[]": metric})
    if not data:
        return []
    labels: set = set()
    for row in data.get("data", []):
        labels.update(row.keys())
    labels.discard("__name__")
    return sorted(labels)


async def _label_values(client: httpx.AsyncClient, base: str, label: str, metric: Optional[str] = None) -> List[str]:
    params = {"match[]": metric} if metric else None
    data = await _get_json(client, base, f"/api/v1/label/{label}/values", params)
    if not data:
        return []
    return [v for v in data.get("data", []) if isinstance(v, str)]


def _looks_like_http_status(values: List[str]) -> bool:
    return bool(values) and all(re.fullmatch(r"\d{3}", v) for v in values)


async def discover_metrics_profile(prometheus_url: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    base = (prometheus_url or "").rstrip("/")
    if not base:
        raise MetricsDiscoveryError("Prometheus URL is required", status_code=400)

    result: Dict[str, Any] = {
        "service_label": {"candidates": [], "note": None},
        "request_metric": {"candidates": [], "note": None},
        "status_label": {"candidates": [], "note": None},
        "latency_histogram": {"candidates": [], "note": None},
        "error_regex": {"suggestion": None, "note": None},
        "cpu_query": {"suggestion": None, "note": None},
        "mem_query": {"suggestion": None, "note": None},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        names_data = await _get_json(client, base, "/api/v1/label/__name__/values")
        if names_data is None:
            raise MetricsDiscoveryError(
                f"Could not reach Prometheus at {base}", status_code=400
            )
        metric_names: List[str] = [n for n in names_data.get("data", []) if isinstance(n, str)]

        request_candidates = _rank_request_metrics(metric_names)
        result["request_metric"]["candidates"] = request_candidates
        if not request_candidates:
            result["request_metric"]["note"] = "No counter metric found containing 'request'"

        histogram_candidates = _rank_latency_histograms(metric_names)
        result["latency_histogram"]["candidates"] = histogram_candidates
        if not histogram_candidates:
            result["latency_histogram"]["note"] = "No histogram metric found containing 'duration'/'latency'"

        top_request_metric = request_candidates[0] if request_candidates else None
        top_status_label: Optional[str] = None

        if top_request_metric:
            series_labels = await _series_labels(client, base, top_request_metric)
            service_candidates = _rank(series_labels, ["service", "app", "job", "deployment", "container"])
            result["service_label"]["candidates"] = service_candidates
            if not service_candidates:
                result["service_label"]["note"] = f"No label on {top_request_metric} looked like a service name"

            status_candidates = _rank(series_labels, ["status", "code", "response_code"])
            result["status_label"]["candidates"] = status_candidates
            if status_candidates:
                top_status_label = status_candidates[0]
            else:
                result["status_label"]["note"] = f"No label on {top_request_metric} looked like a status code"
        else:
            result["service_label"]["note"] = "No request metric found to inspect for labels"
            result["status_label"]["note"] = "No request metric found to inspect for labels"

        if top_status_label:
            values = await _label_values(client, base, top_status_label, top_request_metric)
            if _looks_like_http_status(values) and any(v.startswith("5") for v in values):
                result["error_regex"]["suggestion"] = "5.."
            else:
                result["error_regex"]["note"] = "Status label values don't look like HTTP status codes"
        else:
            result["error_regex"]["note"] = "No status label found to inspect"

        ns_matcher = f'namespace="{namespace}"' if namespace else ""

        cpu_metric = next(
            (n for n in ("container_cpu_usage_seconds_total", "node_cpu_seconds_total") if n in metric_names),
            None,
        )
        if cpu_metric:
            sel = f"{{{ns_matcher}}}" if ns_matcher else ""
            result["cpu_query"]["suggestion"] = f"avg(rate({cpu_metric}{sel}[5m])) * 100"
        else:
            result["cpu_query"]["note"] = "No container/node CPU metric found"

        mem_metric = next(
            (n for n in ("container_memory_usage_bytes", "node_memory_MemAvailable_bytes") if n in metric_names),
            None,
        )
        if mem_metric:
            sel = f"{{{ns_matcher}}}" if ns_matcher else ""
            result["mem_query"]["suggestion"] = f"sum({mem_metric}{sel}) / (1024*1024*1024)"
        else:
            result["mem_query"]["note"] = "No container/node memory metric found"

    return result
