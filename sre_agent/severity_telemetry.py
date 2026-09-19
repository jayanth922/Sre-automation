"""Measure the severity engine's inputs from the cluster's own Prometheus.

Severity decides whether a remediation plan may run without a human, so its
inputs are gathered here, deterministically, rather than left to whichever
tools the model happened to call during the investigation. Before this module
existed they were left to exactly that, and the result was that autonomy was
unreachable in production:

  * `slo_burn_rate` and `error_rate_slope` had no producer anywhere outside
    the test fixtures, and
  * the one real `saturation` producer reported it as
    `{"query": …, "value": …}`, which the extractor could not read as a
    number.

`compute_urgency_score` needs all three, so urgency was always unknown,
severity always escalated to UNKNOWN, and `is_low_severity` was always False.
Every plan needed a human — not because the plans were risky, but because the
gate could not see. The tests all passed, because they hand-injected the three
metrics the real pipeline never produced.

The measurements here are honest or absent. A cluster that has not configured
`saturation_query` / `slo_target` gets exactly the old behaviour (unmeasured →
UNKNOWN → human approval), which is the safe direction to fail.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import httpx

from . import metrics_profile as mp

logger = logging.getLogger(__name__)

# The rate window for every severity query. Long enough that a scrape gap does
# not read as a cliff, short enough to still describe "now".
SEVERITY_WINDOW = "5m"
SEVERITY_WINDOW_MINUTES = 5.0

# Prometheus is in the customer's cluster and severity is on the critical path
# to a gate decision; a hung query must not hold the graph open.
QUERY_TIMEOUT_SECONDS = 10.0


async def _scalar(
    client: httpx.AsyncClient, base: str, promql: str
) -> Tuple[Optional[float], bool]:
    """Run an instant query. Returns (value, reached_prometheus).

    The second element separates "Prometheus answered, nothing matched" from
    "the query never completed". Only the first is evidence of anything.
    """
    try:
        resp = await client.get(f"{base}/api/v1/query", params={"query": promql})
        data = resp.json()
    except Exception as exc:  # network, timeout, malformed JSON
        logger.warning("SeverityTelemetry: query failed (%s): %s", promql, exc)
        return None, False

    if data.get("status") != "success":
        logger.warning(
            "SeverityTelemetry: Prometheus rejected query (%s): %s",
            promql,
            str(data.get("error"))[:200],
        )
        return None, False

    result = (data.get("data") or {}).get("result") or []
    if not result:
        return None, True
    try:
        return float(result[0]["value"][1]), True
    except (KeyError, IndexError, TypeError, ValueError):
        return None, True


def error_ratio(
    errors: Optional[float],
    total: Optional[float],
    *,
    reached: bool,
) -> Optional[float]:
    """Failing fraction of requests, or None when it cannot be known.

    The subtle case is an empty error series against live traffic. Prometheus
    returns nothing for `sum(rate(...{status=~"5.."}))` when no failing series
    exists at all, which looks identical to a failed query — but with a
    measured, positive denominator it is not missing data, it is a measured
    zero: we counted the requests, and none of them failed. Treating that as
    unknown would keep every healthy service permanently unclassifiable,
    which is the bug this module exists to fix.

    No traffic is the opposite case. A zero denominator makes the ratio
    undefined, and inventing 0.0 there would report a calm service that is
    actually receiving nothing at all.
    """
    if not reached or total is None or total <= 0:
        return None
    return (errors or 0.0) / total


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


async def measure_severity_signals(
    *,
    prometheus_url: Optional[str],
    metrics_config: Optional[str],
    service: Optional[str],
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Measure what the severity engine needs for one incident's service.

    Returns ``{"metrics": {...}, "sources": {...}, "service": ..., "window":
    ...}``. Metrics carry canonical names so the existing extractor reads them
    with no special case; sources are kept in a sibling key rather than inline
    so a PromQL string can never be mistaken for a measurement.

    Anything that could not be measured is simply absent — never defaulted —
    so the severity engine goes on treating it as unknown.
    """
    base = (prometheus_url or "").rstrip("/")
    if not base or not service:
        return {}

    try:
        profile = mp.resolve(metrics_config, namespace)
    except mp.MetricsProfileNotConfigured as exc:
        logger.info("SeverityTelemetry: profile not configured (%s)", exc)
        return {}
    except mp.MetricsProfileMalformed as exc:
        logger.warning("SeverityTelemetry: %s", exc)
        return {}

    measured: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    def record(name: str, value: Optional[float], source: str) -> None:
        if value is not None:
            measured[name] = value
            sources[name] = source

    async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_SECONDS) as client:
        q_total = mp.q_sev_requests(profile, service, SEVERITY_WINDOW)
        q_errors = mp.q_sev_errors(profile, service, SEVERITY_WINDOW)
        q_total_prev = mp.q_sev_requests(
            profile, service, SEVERITY_WINDOW, offset=SEVERITY_WINDOW
        )
        q_errors_prev = mp.q_sev_errors(
            profile, service, SEVERITY_WINDOW, offset=SEVERITY_WINDOW
        )

        total, total_ok = await _scalar(client, base, q_total)
        errors, _ = await _scalar(client, base, q_errors)
        rate_now = error_ratio(errors, total, reached=total_ok)
        record("error_rate", rate_now, q_errors)

        # Slope needs both windows; one alone says nothing about direction.
        total_prev, prev_ok = await _scalar(client, base, q_total_prev)
        errors_prev, _ = await _scalar(client, base, q_errors_prev)
        rate_prev = error_ratio(errors_prev, total_prev, reached=prev_ok)
        if rate_now is not None and rate_prev is not None:
            record(
                "error_rate_slope",
                (rate_now - rate_prev) / SEVERITY_WINDOW_MINUTES,
                f"({q_errors}) - ({q_errors_prev})",
            )

        target = mp.slo_target(profile)
        if target is not None and rate_now is not None:
            budget = 1.0 - target
            record("slo_burn_rate", rate_now / budget, f"error_rate / {budget:g}")
            # A bool, not a float, so it bypasses `record`'s numeric contract.
            measured["slo_breached"] = rate_now > budget
            sources["slo_breached"] = f"error_rate > {budget:g}"

        q_sat = mp.q_sev_saturation(profile, service)
        if q_sat:
            raw, _ = await _scalar(client, base, q_sat)
            if raw is not None:
                record("saturation", _clamp01(raw), q_sat)

    if measured:
        logger.info(
            "SeverityTelemetry: measured %s for service=%s",
            ", ".join(f"{k}={v}" for k, v in sorted(measured.items())),
            service,
        )
    else:
        logger.info(
            "SeverityTelemetry: nothing measurable for service=%s; "
            "severity will stay UNKNOWN and the plan will need approval",
            service,
        )

    if not measured:
        return {}
    return {
        "metrics": measured,
        "sources": sources,
        "service": service,
        "window": SEVERITY_WINDOW,
    }


async def measure_for_incident(
    *, cluster_id: Any, service: Optional[str]
) -> Dict[str, Any]:
    """Look up the cluster's Prometheus and profile, then measure.

    A cluster row that is missing, has no Prometheus URL, or has no
    observability profile yields `{}` — the same "unmeasured" state the
    severity engine already handles, not an error that would fail the graph.
    """
    import uuid as _uuid

    from backend import database, models

    if not service or not cluster_id:
        return {}
    try:
        key = _uuid.UUID(str(cluster_id))
    except (TypeError, ValueError):
        return {}

    async with database.AsyncSessionLocal() as db:
        cluster = await db.get(models.Cluster, key)
        if cluster is None:
            return {}
        prometheus_url = cluster.prometheus_url
        metrics_config = cluster.metrics_config
        namespace = cluster.namespace

    return await measure_severity_signals(
        prometheus_url=prometheus_url,
        metrics_config=metrics_config,
        service=service,
        namespace=namespace,
    )
