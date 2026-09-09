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

# Which Prometheus label carries the Kubernetes namespace. Not customer-
# configurable (no Settings field exists for it): Prometheus's own Kubernetes
# service-discovery relabeling universally emits this label as `namespace`,
# so this is a k8s-wide convention rather than a per-app guess.
NAMESPACE_LABEL = "namespace"


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
