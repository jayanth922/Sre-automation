"""Per-cluster observability query profile.

The platform must not assume any one workload's metric schema. A cluster can
carry a `metrics_config` (JSON) describing its Prometheus conventions; when
absent we fall back to widely-used defaults. All PromQL is built from the
resolved profile, so the same code serves any customer's metrics.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

# Defaults follow common Prometheus / OpenMetrics conventions. They are NOT
# specific to the bundled demo — any workload using a request counter with a
# status label and an RED-style latency histogram works out of the box.
DEFAULTS: Dict[str, str] = {
    "service_label": "service",
    "request_metric": "http_requests_total",
    "status_label": "status",
    "error_regex": "5..",
    "latency_histogram": "http_request_duration_seconds",  # `_bucket` appended
    "cpu_query": "avg(rate(container_cpu_usage_seconds_total[5m])) * 100",
    "mem_query": "sum(container_memory_usage_bytes) / (1024*1024*1024)",
    # Which Prometheus label carries the namespace (for scoped clusters).
    "namespace_label": "namespace",
}

CONFIGURABLE_KEYS = list(DEFAULTS.keys())


def resolve(raw: Optional[str], namespace: Optional[str] = None) -> Dict[str, str]:
    """Merge a cluster's stored JSON config over the defaults, and record the
    cluster's namespace scope (if any) so queries can be filtered to it."""
    cfg = dict(DEFAULTS)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for key in CONFIGURABLE_KEYS:
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        cfg[key] = val.strip()
        except (json.JSONDecodeError, TypeError):
            pass
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
