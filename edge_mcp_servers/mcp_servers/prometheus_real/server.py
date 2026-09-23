#!/usr/bin/env python3
"""
Real Prometheus MCP Server

This MCP server directly queries Prometheus using the prometheus_api_client
library instead of calling mock APIs. It provides production-ready metrics
querying through the Model Context Protocol.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP
from prometheus_api_client import PrometheusConnect
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize Prometheus client
prom_client = None
last_connection_attempt = 0
CONNECTION_RETRY_INTERVAL = 10  # Seconds between retries

# A high-cardinality query (broad label matcher) crossed with a fine step over
# a long window can return thousands of series x thousands of points each —
# one real run blew a 1M-token LLM context by 2.4x from a single tool result.
# Cap what goes back to the agent; note when we've cut something so it knows
# the data is a sample, not the full series.
MAX_METRIC_SERIES = 20
MAX_POINTS_PER_SERIES = 120


def _downsample_range_result(result: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cap series count and points-per-series on a Prometheus range-query result."""
    total_series = len(result)
    series_truncated = total_series > MAX_METRIC_SERIES
    kept = result[:MAX_METRIC_SERIES]

    points_truncated = False
    downsampled = []
    for series in kept:
        values = series.get("values", [])
        if len(values) > MAX_POINTS_PER_SERIES:
            points_truncated = True
            # Evenly-spaced subsample so the shape of the series over time
            # is preserved instead of just keeping the earliest window.
            step = len(values) / MAX_POINTS_PER_SERIES
            values = [values[int(i * step)] for i in range(MAX_POINTS_PER_SERIES)]
        downsampled.append({**series, "values": values})

    return {
        "result": downsampled,
        "series_returned": len(downsampled),
        "series_total": total_series,
        "series_truncated": series_truncated,
        "points_per_series_truncated": points_truncated,
        "points_per_series_cap": MAX_POINTS_PER_SERIES,
    }


# get_metric_range downsamples via _downsample_range_result above, but an
# instant/vector query (get_metric) has no time-axis to downsample — an
# under-filtered PromQL query can still match thousands of series and each
# one carries a full label set, so this caps series count the same way.
MAX_INSTANT_SERIES = 50


def _cap_vector_result(result: Any) -> Dict[str, Any]:
    """Cap series count (and, as a last resort, serialized size) on an
    instant PromQL query result so a broad/unfiltered match can't blow the
    LLM context the way an uncapped range query once did."""
    if not isinstance(result, list):
        return {"result": result}

    total = len(result)
    kept = result[:MAX_INSTANT_SERIES]
    payload = {
        "result": kept,
        "series_returned": len(kept),
        "series_total": total,
        "series_truncated": total > MAX_INSTANT_SERIES,
    }

    max_chars = 200_000
    while True:
        text = json.dumps(payload, separators=(",", ":"), default=str)
        if len(text) <= max_chars or len(payload["result"]) <= 1:
            return payload
        payload["result"] = payload["result"][: max(1, len(payload["result"]) // 2)]
        payload["series_truncated"] = True


def get_prom_client() -> Optional[PrometheusConnect]:
    """
    Get Prometheus client, attempting to initialize if necessary.
    Implements lazy loading and backoff to handle startup race conditions.
    """
    global prom_client, last_connection_attempt
    
    if prom_client:
        return prom_client
        
    # Check if we should retry
    now = time.time()
    if now - last_connection_attempt < CONNECTION_RETRY_INTERVAL:
        logger.warning(f"⚠️ Prometheus client not ready, waiting for retry interval ({int(CONNECTION_RETRY_INTERVAL - (now - last_connection_attempt))}s)")
        return None
        
    last_connection_attempt = now
    
    prometheus_url = os.getenv("PROMETHEUS_URL")
    if not prometheus_url:
        logger.warning("⚠️ PROMETHEUS_URL not set, server will not function")
        return None

    try:
        logger.info(f"🔄 Attempting to connect to Prometheus at {prometheus_url}...")
        client = PrometheusConnect(url=prometheus_url, disable_ssl=False)
        # Test connection
        if client.check_prometheus_connection():
            logger.info(f"✅ Connected to Prometheus at {prometheus_url}")
            prom_client = client
            return prom_client
        else:
            logger.error(f"❌ Connection check failed for {prometheus_url}")
            return None
    except Exception as e:
        logger.error(f"❌ Failed to connect to Prometheus: {e}")
        return None


def _coerce_with_reason(
    value: Union[str, int, float, datetime, None]
) -> "tuple[datetime, Optional[str]]":
    """Coerce an LLM-supplied time argument, and say when the coercion gave up.

    The prometheus_api_client library expects datetime objects for
    custom_query_range (it calls .timestamp() on them). LLMs pass strings
    (RFC3339, unix epoch, relative like "5m"/"1h"), which previously
    triggered "'str' object has no attribute 'timestamp'" 500-style errors.
    This helper accepts any of the common formats and always returns a
    timezone-aware datetime (UTC).

    The last-resort fallback is now(), so a malformed argument cannot crash the
    call. That is the right behaviour and the wrong silence: the caller asked
    about one moment and was answered about another, with nothing in the result
    saying so. Return the reason alongside, so callers can put it in the result.
    """
    if value is None or value == "":
        return datetime.now(timezone.utc), None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)), None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc), None
    if isinstance(value, str):
        s = value.strip()
        # Relative shorthand: "5m", "1h", "30s", "2h30m"
        if s and s[0] != "-" and s[-1] in {"s", "m", "h", "d"}:
            try:
                total_seconds = 0
                num = ""
                for ch in s:
                    if ch.isdigit():
                        num += ch
                    elif ch in {"s", "m", "h", "d"} and num:
                        n = int(num)
                        total_seconds += n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[ch]
                        num = ""
                if total_seconds > 0:
                    return (
                        datetime.now(timezone.utc) - timedelta(seconds=total_seconds),
                        None,
                    )
            except Exception:
                pass
        # Unix timestamp
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc), None
        except (ValueError, OSError):
            pass
        # RFC3339 / ISO-8601
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)), None
        except ValueError:
            pass
    # Last resort: now() so the call doesn't crash; the agent will see
    # an empty result rather than a tool error.
    logger.warning(f"Could not coerce {value!r} to datetime; defaulting to now().")
    return datetime.now(timezone.utc), f"could not parse time={value!r}"


def _coerce_to_datetime(value: Union[str, int, float, datetime, None]) -> datetime:
    """Coerce LLM-supplied time arguments into a real datetime."""
    resolved, _reason = _coerce_with_reason(value)
    return resolved


def _sample_timestamp(result: Any) -> Optional[float]:
    """The instant Prometheus itself stamped on the sample it returned.

    This is the authoritative answer to "when is this value from" — better than
    the client's clock, and the only way to see that a query answered about a
    moment other than the one that was asked for.
    """
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    if isinstance(value, (list, tuple)) and value:
        try:
            return float(value[0])
        except (TypeError, ValueError):
            return None
    return None


def _evaluation_stamp(
    result: Any,
    *,
    time_argument: Any,
    requested: Optional[datetime],
    coerce_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Say which single moment an instant query actually answered about.

    An instant vector is a number with no visible timestamp once it reaches the
    agent's transcript. A specialist reconstructing an incident that ended ten
    minutes ago, who omits `time`, is handed the value NOW and has nothing in
    the result to warn them it is not the incident's value. Trial 5 did exactly
    that and reported "we're missing the live measurement" while holding one.
    """
    stamp: Dict[str, Any] = {"time_argument": time_argument or None}

    sampled = _sample_timestamp(result)
    if sampled is not None:
        moment, source = sampled, "prometheus sample"
    elif requested is not None:
        moment, source = requested.timestamp(), "requested time (query returned no sample)"
    else:
        moment, source = datetime.now(timezone.utc).timestamp(), (
            "client clock (query returned no sample)"
        )
    stamp["evaluated_at"] = datetime.fromtimestamp(moment, timezone.utc).isoformat()
    stamp["evaluated_at_source"] = source

    if coerce_error:
        stamp["warning"] = (
            f"TIME ARGUMENT IGNORED: {coerce_error}. This is the value NOW, not at "
            f"the moment you asked for, so it is NOT evidence about a past window. "
            f"Re-run with an RFC3339 timestamp (2026-09-23T02:32:08Z) or a unix "
            f"epoch."
        )
    elif not time_argument:
        stamp["warning"] = (
            "No time= was given, so this is the value NOW. If you are "
            "investigating a window that has already passed, this number does "
            "not describe that window and is not evidence about it. Re-run with "
            "time=<RFC3339 at the incident>, or use get_metric_range to see the "
            "window itself."
        )
    return stamp


# Create FastMCP server with host/port from environment
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("prometheus-real-mcp-server", host=host, port=port)


# Tool implementations

@mcp.tool()
async def check_prometheus_health() -> str:
    """
    Check the health of the Prometheus connection.
    Returns the status and URL being used.
    """
    client = get_prom_client()
    url = os.getenv("PROMETHEUS_URL", "NOT_SET")
    
    if client:
        return json.dumps({
            "status": "healthy",
            "url": url,
            "message": "Connected to Prometheus"
        }, separators=(",", ":"))
    else:
        return json.dumps({
            "status": "unhealthy",
            "url": url,
            "message": "Failed to connect to Prometheus. Check PROMETHEUS_URL and network connectivity."
        }, separators=(",", ":"))

@mcp.tool()
async def get_metric(query: str, time: str = None) -> str:
    """
    Query a Prometheus metric using PromQL at a single instant.
    
    Args:
        query: PromQL query string (e.g., 'cpu_usage{namespace="production"}')
        time: RFC3339 timestamp or unix timestamp. Omit it ONLY when you want
            the value right now. When you are reconstructing an incident that
            has already passed, pass the incident's timestamp — without it you
            get the value NOW, which is not evidence about that window.

    The result always carries `evaluated_at`: the instant the returned sample is
    stamped with. A value can then never be read as describing a moment it does
    not describe.
    """
    client = get_prom_client()
    if not client:
        return "Error: Could not connect to Prometheus. Please check infrastructure status."

    logger.info(f"Querying Prometheus: {query} (time={time})")

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        # prometheus_api_client.custom_query(query, params=None) takes a
        # DICT for params (positional second arg). Passing `time` directly
        # was producing "'str' object is not a mapping" errors. The HTTP
        # /api/v1/query endpoint accepts a `time=` query param (unix epoch).
        requested_dt = None
        coerce_error = None
        if time:
            requested_dt, coerce_error = _coerce_with_reason(time)
            params = {"time": str(int(requested_dt.timestamp()))}
            result = await loop.run_in_executor(
                None, lambda: client.custom_query(query, params=params)
            )
        else:
            result = await loop.run_in_executor(None, client.custom_query, query)

        payload = _cap_vector_result(result)
        payload.update(
            _evaluation_stamp(
                result,
                time_argument=time,
                requested=requested_dt,
                coerce_error=coerce_error,
            )
        )
        return json.dumps(payload, separators=(",", ":"), default=str)
    except Exception as e:
        # Try to expose HTTP status / body so the agent can distinguish
        # "your PromQL is malformed" (400/422 from Prometheus) from
        # "Prometheus is unreachable" (connection error / 5xx). Without
        # this the agent only saw a generic "Error" string and tended to
        # blame "monitoring is broken".
        status = getattr(getattr(e, "response", None), "status_code", None)
        body = ""
        try:
            body = getattr(e, "response", None).text[:300] if getattr(e, "response", None) else ""
        except Exception:
            body = ""
        logger.error(f"Error querying metric (status={status}, body={body!r}): {e}")
        if status and 400 <= status < 500:
            return (
                f"Error querying metric: query was rejected by Prometheus "
                f"(HTTP {status}). The PromQL is likely malformed or uses "
                f"unknown labels. query={query!r}; body={body[:200]!r}"
            )
        if status and status >= 500:
            return (
                f"Error querying metric: Prometheus returned HTTP {status} "
                f"(server-side issue, NOT a problem with the metrics pipeline). "
                f"query={query!r}; body={body[:200]!r}"
            )
        return f"Error querying metric: {e}"


@mcp.tool()
async def get_metric_range(query: str, start_time: str, end_time: str, step: str = "15s") -> str:
    """
    Query a Prometheus metric over a time range using PromQL. Returns time series data.
    
    Args:
        query: PromQL query string
        start_time: Start time (RFC3339 or unix timestamp)
        end_time: End time (RFC3339 or unix timestamp)
        step: Query resolution step width (default: 15s)
    """
    client = get_prom_client()
    if not client:
        return "Error: Could not connect to Prometheus. Please check infrastructure status."

    # The prometheus_api_client library expects datetime objects for
    # start_time / end_time and calls .timestamp() on them. LLMs pass
    # strings (RFC3339, "5m", unix epoch), so we coerce here.
    start_dt = _coerce_to_datetime(start_time)
    end_dt = _coerce_to_datetime(end_time)

    logger.info(
        f"Querying Prometheus range: {query} from {start_dt.isoformat()} "
        f"to {end_dt.isoformat()} (step={step})"
    )

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            client.custom_query_range,
            query,
            start_dt,
            end_dt,
            step,
        )
        payload = _downsample_range_result(result)
        text = json.dumps(payload, separators=(",", ":"), default=str)
        # Last-resort safety net: even a capped series/points count can be
        # huge if label sets are verbose. Hard-cap the serialized size too.
        max_chars = 200_000
        if len(text) > max_chars:
            payload["result"] = payload["result"][: max(1, len(payload["result"]) // 2)]
            payload["series_truncated"] = True
            text = json.dumps(payload, separators=(",", ":"), default=str)[:max_chars]
        return text
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        body = ""
        try:
            body = getattr(e, "response", None).text[:300] if getattr(e, "response", None) else ""
        except Exception:
            body = ""
        logger.error(
            f"Error querying metric range (status={status}, body={body!r}): {e}"
        )
        if status and 400 <= status < 500:
            return (
                f"Error querying metric range: query rejected by Prometheus "
                f"(HTTP {status}). PromQL is likely malformed or label set is wrong. "
                f"query={query!r}; body={body[:200]!r}"
            )
        if status and status >= 500:
            return (
                f"Error querying metric range: Prometheus returned HTTP {status} "
                f"(server-side issue, NOT a metrics-pipeline failure). "
                f"query={query!r}; body={body[:200]!r}"
            )
        return f"Error querying metric range: {e}"


@mcp.tool()
async def get_golden_signals(service: str, namespace: str = None, time: str = None) -> str:
    """
    Get Golden Signals (Latency, Traffic, Errors, Saturation) for a service.

    Metric names are configurable via environment variables:
    - PROM_LATENCY_METRIC: histogram metric for latency (default: http_request_duration_seconds_bucket)
    - PROM_TRAFFIC_METRIC: counter metric for traffic (default: http_requests_total)
    - PROM_CPU_METRIC: gauge metric for CPU saturation (default: container_cpu_usage_seconds_total)
    - PROM_SERVICE_LABEL: label name for service filtering (default: service)

    Args:
        service: Service name to query
        namespace: Namespace (optional)
        time: RFC3339 or unix timestamp. Omit it only for "right now" — when
            reconstructing a past incident, pass its timestamp. The result
            carries `query_scope.evaluated_at` saying which instant you got.
    """
    client = get_prom_client()
    if not client:
        return "Error: Could not connect to Prometheus. Please check infrastructure status."

    logger.info(f"Getting Golden Signals for service: {service}")

    # Configurable metric names — adapt to any Prometheus deployment
    latency_metric = os.getenv("PROM_LATENCY_METRIC", "http_request_duration_seconds_bucket")
    traffic_metric = os.getenv("PROM_TRAFFIC_METRIC", "http_requests_total")
    cpu_metric = os.getenv("PROM_CPU_METRIC", "container_cpu_usage_seconds_total")
    service_label = os.getenv("PROM_SERVICE_LABEL", "service")

    namespace_filter = f',namespace="{namespace}"' if namespace else ""

    # Build PromQL queries for Golden Signals
    queries = {
        "latency": f'histogram_quantile(0.99, rate({latency_metric}{{{service_label}="{service}"{namespace_filter}}}[5m]))',
        "traffic": f'sum(rate({traffic_metric}{{{service_label}="{service}"{namespace_filter}}}[5m]))',
        "errors": f'sum(rate({traffic_metric}{{{service_label}="{service}",status=~"5.."{namespace_filter}}}[5m]))',
        "saturation": f'avg({cpu_metric}{{pod=~"{service}.*"{namespace_filter}}})',
    }

    # Query all signals
    loop = asyncio.get_event_loop()
    results = {}
    time_params = None
    requested_dt = None
    coerce_error = None
    if time:
        try:
            requested_dt, coerce_error = _coerce_with_reason(time)
            time_params = {"time": str(int(requested_dt.timestamp()))}
        except Exception as coerce_err:
            coerce_error = f"could not parse time={time!r} ({coerce_err})"
            logger.warning(f"Could not coerce time={time!r}: {coerce_err}")
    first_sample: Any = None
    for signal_name, query in queries.items():
        try:
            if time_params:
                result = await loop.run_in_executor(
                    None, lambda q=query: client.custom_query(q, params=time_params)
                )
            else:
                result = await loop.run_in_executor(None, client.custom_query, query)
            if first_sample is None and _sample_timestamp(result) is not None:
                first_sample = result
            results[signal_name] = {
                "query": query,
                "value": _cap_vector_result(result),
            }
        except Exception as e:
            logger.warning(f"Failed to query {signal_name}: {e}")
            results[signal_name] = {
                "query": query,
                "error": str(e),
            }

    results["query_scope"] = _evaluation_stamp(
        first_sample,
        time_argument=time,
        requested=requested_dt,
        coerce_error=coerce_error,
    )
    return json.dumps(results, separators=(",", ":"), default=str)


@mcp.tool()
async def list_metric_names() -> str:
    """
    List every metric name currently exposed by this Prometheus (its live
    catalog), so a caller can validate a generated PromQL query against what
    this cluster actually has rather than a fixed guess.
    """
    client = get_prom_client()
    if not client:
        return json.dumps({"metric_names": [], "error": "Could not connect to Prometheus"})

    try:
        loop = asyncio.get_event_loop()
        names = await loop.run_in_executor(None, client.all_metrics)
        return json.dumps({"metric_names": sorted(names)})
    except Exception as e:
        logger.warning(f"Failed to list metric names: {e}")
        return json.dumps({"metric_names": [], "error": str(e)})


@mcp.tool()
async def validate_promql_syntax(query: str) -> str:
    """
    Validate a PromQL query's grammar using Prometheus's own parser
    (GET /api/v1/format_query), rather than a regex approximation. Returns
    {"valid": true} on success, or {"valid": false, "error": "..."} with
    Prometheus's own parse error otherwise.
    """
    prometheus_url = os.getenv("PROMETHEUS_URL")
    if not prometheus_url:
        return json.dumps({"valid": False, "error": "PROMETHEUS_URL not set"})

    try:
        import requests

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: requests.get(
                f"{prometheus_url.rstrip('/')}/api/v1/format_query",
                params={"query": query},
                timeout=10,
            ),
        )
        if resp.status_code == 200:
            return json.dumps({"valid": True})
        try:
            body = resp.json()
            error = body.get("error", resp.text)
        except ValueError:
            error = resp.text
        return json.dumps({"valid": False, "error": error})
    except Exception as e:
        logger.warning(f"PromQL syntax validation request failed: {e}")
        return json.dumps({"valid": False, "error": f"validation request failed: {e}"})


if __name__ == "__main__":
    logger.info("Starting FastMCP server execution...")
    
    # Try initial connection non-blocking intended, but get_prom_client is sync for now
    # We can try once at startup to warm up
    get_prom_client()
    
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
