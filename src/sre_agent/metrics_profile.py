"""Per-cluster observability query profile.

The platform must not assume any one workload's metric schema. A cluster's
`metrics_config` (JSON) states its Prometheus conventions explicitly — there
is no platform-wide fallback, because silently substituting a guessed metric
name can return a confident-looking but wrong (or empty) result instead of an
honest "not configured". A cluster with an incomplete profile fails loudly
(`MetricsProfileNotConfigured`) rather than running queries built on guesses.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

# Every field a cluster must set explicitly in Settings -> AI & metrics before
# any Prometheus-backed query runs for it. (EXAMPLES below are shown in the UI
# as placeholder text only — they are never substituted into a real query.)
REQUIRED_KEYS: List[str] = [
    "service_label",
    "request_metric",
    "status_label",
    "error_regex",
    "latency_histogram",  # `_bucket` appended
    "cpu_query",
    "mem_query",
]

EXAMPLES: Dict[str, str] = {
    "service_label": "service",
    "request_metric": "http_requests_total",
    "status_label": "status",
    "error_regex": "5..",
    "latency_histogram": "http_request_duration_seconds",
    "cpu_query": "avg(rate(container_cpu_usage_seconds_total[5m])) * 100",
    "mem_query": "sum(container_memory_usage_bytes) / (1024*1024*1024)",
}

# Extra fields the severity engine needs before it can classify urgency.
# Deliberately optional and deliberately separate from `cpu_query`.
#
# `cpu_query` cannot be reused for saturation: it is customer-authored with no
# declared unit (the example above returns a percentage, others return a
# ratio), while the severity engine wants 0.0–1.0. Guessing wrong by 100x in
# the low direction would understate urgency and make a plan *more* likely to
# run unattended, so saturation gets its own field with its unit stated rather
# than a conversion inferred from a number's magnitude.
#
# A cluster that sets neither keeps today's behaviour exactly: urgency stays
# unmeasured, severity escalates to UNKNOWN, and every plan needs a human.
# That is the safe direction to fail, so these stay optional.
SEVERITY_KEYS: List[str] = ["saturation_query", "slo_target"]

SEVERITY_EXAMPLES: Dict[str, str] = {
    # Must return 0.0–1.0 (a ratio, not a percentage), and should return one
    # series. `$service` is substituted with the alerting service's name, so
    # the reading describes the service that is actually on fire rather than
    # a cluster-wide average that dilutes it. A query without `$service` is
    # used verbatim.
    "saturation_query": (
        'avg(rate(process_cpu_seconds_total{job="$service"}[5m])) '
        '/ avg(kube_pod_container_resource_limits{resource="cpu",container="$service"})'
    ),
    # Availability target as a fraction, e.g. 0.99 for "99% of requests
    # succeed". The error budget is 1 - slo_target.
    "slo_target": "0.99",
}

# Which Prometheus label carries the Kubernetes namespace. Not customer-
# configurable (no Settings field exists for it): Prometheus's own Kubernetes
# service-discovery relabeling universally emits this label as `namespace`,
# so this is a k8s-wide convention rather than a per-app guess.
NAMESPACE_LABEL = "namespace"


class MetricsProfileMalformed(ValueError):
    """Raised when an optional profile field is present but unusable.

    Distinct from `MetricsProfileNotConfigured`: "you did not set this" is a
    normal state with a safe default, while "you set this to something I
    cannot read" is a configuration error that must be surfaced rather than
    silently treated as absent.
    """

    def __init__(self, key: str, value: object, expectation: str):
        self.key = key
        self.value = value
        super().__init__(
            f"Observability profile field {key!r} is malformed "
            f"({value!r}): {expectation}"
        )


class MetricsProfileNotConfigured(ValueError):
    """Raised when a cluster's observability profile is missing required
    fields — callers must surface this as a clear "not configured" error,
    never swallow it and run queries against guessed metric names."""

    def __init__(self, missing: List[str]):
        self.missing = missing
        super().__init__(
            "Observability profile is not configured: missing " + ", ".join(missing)
        )


def resolve(raw: Optional[str], namespace: Optional[str] = None) -> Dict[str, str]:
    """Build this cluster's observability profile strictly from its stored
    config. Raises MetricsProfileNotConfigured if any required field is
    missing — never fills gaps with a guessed default."""
    cfg: Dict[str, str] = {}
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            for key in REQUIRED_KEYS:
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    cfg[key] = val.strip()

    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise MetricsProfileNotConfigured(missing)

    # Severity fields are optional, so an absent one is not an error — but a
    # present one that will not parse is. Dropping a malformed `slo_target`
    # silently would leave the burn rate unmeasured with no indication why.
    if isinstance(data, dict):
        for key in SEVERITY_KEYS:
            val = data.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            if key == "slo_target":
                try:
                    target = float(val)
                except (TypeError, ValueError):
                    raise MetricsProfileMalformed(
                        key, val, "expected a number such as 0.99"
                    )
                if not 0.0 < target < 1.0:
                    raise MetricsProfileMalformed(
                        key, val, "expected a fraction strictly between 0 and 1"
                    )
                cfg[key] = str(target)
            elif isinstance(val, str):
                cfg[key] = val.strip()

    cfg["namespace_label"] = NAMESPACE_LABEL
    cfg["namespace"] = (namespace or "").strip()
    return cfg


def _ns_matcher(c: Dict[str, str]) -> str:
    """`namespace="x"` when the cluster is namespace-scoped, else empty."""
    ns = c.get("namespace") or ""
    return f'{c.get("namespace_label", "namespace")}="{ns}"' if ns else ""


def _sel(c: Dict[str, str], *extra: str) -> str:
    """Build a `{...}` label selector, always including the namespace scope."""
    parts = [p for p in (_ns_matcher(c), *extra) if p]
    return "{" + ",".join(parts) + "}" if parts else ""


# ── Per-service (RED) ────────────────────────────────────────────────────────
def q_service_rps(c: Dict[str, str]) -> str:
    return f"sum by ({c['service_label']}) (rate({c['request_metric']}{_sel(c)}[1m]))"


def q_service_total(c: Dict[str, str]) -> str:
    return f"sum by ({c['service_label']}) (rate({c['request_metric']}{_sel(c)}[5m]))"


def q_service_errors(c: Dict[str, str]) -> str:
    sel = _sel(c, f'{c["status_label"]}=~"{c["error_regex"]}"')
    return f'sum by ({c["service_label"]}) (rate({c["request_metric"]}{sel}[5m]))'


def q_service_latency(c: Dict[str, str], quantile: float) -> str:
    return f"histogram_quantile({quantile}, sum by ({c['service_label']}, le) (rate({c['latency_histogram']}_bucket{_sel(c)}[5m]))) * 1000"


# ── Cluster-wide golden signals ──────────────────────────────────────────────
def q_error_rate(c: Dict[str, str]) -> str:
    err = _sel(c, f'{c["status_label"]}=~"{c["error_regex"]}"')
    return (
        f'sum(rate({c["request_metric"]}{err}[5m]))'
        f" / sum(rate({c['request_metric']}{_sel(c)}[5m])) * 100"
    )


def q_latency_p95(c: Dict[str, str]) -> str:
    return f"histogram_quantile(0.95, sum(rate({c['latency_histogram']}_bucket{_sel(c)}[5m])) by (le)) * 1000"


def q_cpu(c: Dict[str, str]) -> str:
    return c["cpu_query"]


def q_mem(c: Dict[str, str]) -> str:
    return c["mem_query"]


# ── Severity inputs, scoped to the incident's own service ────────────────────
# These feed the severity engine, so they are deliberately narrow: one
# service, one window, and the numerator/denominator kept apart. A single
# PromQL division would collapse "no requests at all" and "requests, none of
# them failing" into the same empty result, and those two mean opposite
# things — see `severity_telemetry.error_ratio`.
def q_sev_requests(
    c: Dict[str, str], service: str, window: str, offset: Optional[str] = None
) -> str:
    sel = _sel(c, f'{c["service_label"]}="{service}"')
    return f"sum(rate({c['request_metric']}{sel}[{window}]{_offset(offset)}))"


def q_sev_errors(
    c: Dict[str, str], service: str, window: str, offset: Optional[str] = None
) -> str:
    sel = _sel(
        c,
        f'{c["service_label"]}="{service}"',
        f'{c["status_label"]}=~"{c["error_regex"]}"',
    )
    return f"sum(rate({c['request_metric']}{sel}[{window}]{_offset(offset)}))"


def _offset(offset: Optional[str]) -> str:
    return f" offset {offset}" if offset else ""


SERVICE_PLACEHOLDER = "$service"


def q_sev_saturation(c: Dict[str, str], service: str = "") -> Optional[str]:
    """The cluster's own saturation expression, or None if it set none.

    `$service` is substituted so the reading is scoped to the service the
    incident is about. Substitution is textual because the expression is the
    cluster's, not ours to parse — an operator who wants a cluster-wide
    number simply omits the placeholder.
    """
    query = c.get("saturation_query")
    if not query:
        return None
    if SERVICE_PLACEHOLDER not in query:
        return query
    # A scoped query with nothing to scope it to is not a query: sending the
    # placeholder through would be a PromQL parse error, and blanking it would
    # silently widen the match.
    return query.replace(SERVICE_PLACEHOLDER, service) if service else None


def slo_target(c: Dict[str, str]) -> Optional[float]:
    """Availability target as a fraction, or None if the cluster set none."""
    raw = c.get("slo_target")
    return float(raw) if raw else None
