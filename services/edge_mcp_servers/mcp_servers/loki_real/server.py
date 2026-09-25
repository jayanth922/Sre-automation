#!/usr/bin/env python3
"""
Real Loki MCP Server (Native FastMCP)

This MCP server directly queries Grafana Loki using the HTTP API.
Uses standard mcp.server.fastmcp implementation.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Loki configuration
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
LOKI_QUERY_ENDPOINT = f"{LOKI_URL}/loki/api/v1/query_range"
LOKI_LABELS_ENDPOINT = f"{LOKI_URL}/loki/api/v1/labels"
LOKI_LABEL_VALUES_ENDPOINT = LOKI_URL + "/loki/api/v1/label/{name}/values"

# Initialize FastMCP server
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("Loki Logs", host=host, port=port)

MAX_MESSAGE_CHARS = 2000
MAX_RESPONSE_CHARS = 200_000


def _cap_logs(logs: list, total_count: int) -> dict:
    """Cap per-message length and total serialized size so a crash-looping
    pod spewing large repeated stack traces can't blow the LLM context."""
    capped = []
    message_truncated = False
    for entry in logs:
        msg = entry.get("message", "")
        if len(msg) > MAX_MESSAGE_CHARS:
            entry = {**entry, "message": msg[:MAX_MESSAGE_CHARS] + " …[truncated]"}
            message_truncated = True
        capped.append(entry)

    logs_truncated = False
    while True:
        payload = {
            "logs": capped,
            "count": total_count,
            "logs_returned": len(capped),
            "logs_truncated": logs_truncated,
            "message_truncated": message_truncated,
        }
        if len(json.dumps(payload, separators=(",", ":"))) <= MAX_RESPONSE_CHARS or len(capped) <= 1:
            return payload
        capped = capped[: max(1, len(capped) // 2)]
        logs_truncated = True


_EMPTY_RESULT_KEYS = (
    "streams_matched",
    "selector_valid",
    "empty_result_reason",
    "invalid_label",
    "invalid_value",
    "available_labels",
    "available_values",
    "warning",
    "note",
)

_MATCHER_RE = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


def _split_selector(logql: str) -> str:
    """Return the bare stream selector - the outermost {...} - dropping every
    pipeline stage after it. Returns "" if there is no parsable selector."""
    start = logql.find("{")
    if start == -1:
        return ""
    in_quotes = False
    escaped = False
    for idx in range(start, len(logql)):
        char = logql[idx]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == "}" and not in_quotes:
            return logql[start : idx + 1]
    return ""


def _get_json(url: str, params: dict) -> Optional[dict]:
    """Best-effort GET. Returns None on any transport or decode failure so the
    caller reports "could not tell" rather than inventing a verdict."""
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def _diagnose_empty_result(logql: str, start_ns: int, end_ns: int) -> dict:
    """Decide *why* a query came back empty, so the caller never has to guess.

    An empty Loki result means one of two opposite things: the query was broken
    (a label name or value that does not exist can never match a stream), or the
    query was sound and nothing was logged. Only the second is evidence. This
    probes Loki's own label index to say which one happened, and phrases the
    broken case so it cannot be read as silence.
    """
    selector = _split_selector(logql)
    if not selector:
        return {
            "selector_valid": None,
            "empty_result_reason": "unparsed_selector",
            "warning": (
                "Query returned no lines and its stream selector could not be "
                "parsed, so it is unknown whether the query was even valid. Do "
                "NOT treat this as evidence that the service was quiet."
            ),
        }

    window = {"start": start_ns, "end": end_ns}

    # If the query carries filters, ask whether the selector *alone* matches
    # anything. If it does, the filters did the excluding and the emptiness is
    # a real observation about the logs.
    if selector != logql.strip():
        probe = _get_json(
            LOKI_QUERY_ENDPOINT, {**window, "query": selector, "limit": 1}
        )
        if probe and probe.get("data", {}).get("result"):
            return {
                "selector_valid": True,
                "empty_result_reason": "filters_excluded_all_lines",
                "note": (
                    f"The stream selector {selector} does match live streams in "
                    "this window; the filters after it excluded every line. This "
                    "IS genuine evidence that no matching line was logged."
                ),
            }

    # Nothing matched even the bare selector. Ask Loki whether that is because
    # the selector is wrong or because the streams are genuinely silent.
    labels_payload = _get_json(LOKI_LABELS_ENDPOINT, window)
    known_labels = (labels_payload or {}).get("data")
    if not known_labels:
        return {
            "selector_valid": None,
            "empty_result_reason": "label_probe_unavailable",
            "warning": (
                "Query matched zero streams, and Loki's label index could not be "
                "read to say why. Do NOT treat this as evidence of silence; "
                "verify the selector or retry before drawing any conclusion."
            ),
        }

    matchers = _MATCHER_RE.findall(selector)
    available = ", ".join(sorted(known_labels))

    for name, _operator, _value in matchers:
        if name not in known_labels:
            return {
                "selector_valid": False,
                "empty_result_reason": "unknown_label",
                "invalid_label": name,
                "available_labels": sorted(known_labels),
                "warning": (
                    f"INVALID QUERY, NOT EVIDENCE: label '{name}' does not exist "
                    f"in Loki for this window, so {selector} can never match a "
                    "stream no matter what the service logged. This result is "
                    "NOT evidence that the service was quiet. Available labels: "
                    f"{available}. Re-run with a valid label before drawing any "
                    "conclusion from the logs."
                ),
            }

    for name, operator, value in matchers:
        if operator != "=":
            continue  # only exact matches are checkable against the value index
        values_payload = _get_json(
            LOKI_LABEL_VALUES_ENDPOINT.format(name=name), window
        )
        known_values = (values_payload or {}).get("data")
        if known_values and value not in known_values:
            shown = ", ".join(sorted(known_values)[:20])
            return {
                "selector_valid": False,
                "empty_result_reason": "unknown_label_value",
                "invalid_label": name,
                "invalid_value": value,
                "available_values": sorted(known_values)[:50],
                "warning": (
                    f"INVALID QUERY, NOT EVIDENCE: label '{name}' has no value "
                    f"'{value}' in this window, so {selector} can never match a "
                    "stream no matter what the service logged. This result is "
                    f"NOT evidence that the service was quiet. Values for "
                    f"'{name}': {shown}. Re-run with a valid value before "
                    "drawing any conclusion from the logs."
                ),
            }

    return {
        "selector_valid": True,
        "empty_result_reason": "no_lines_in_window",
        "note": (
            f"Every label in {selector} exists in Loki for this window and no "
            "line was logged against it. This IS genuine evidence of silence."
        ),
    }


def _parse_time(time_str: Optional[str]) -> int:
    """
    Parse time string to nanoseconds since epoch.
    
    Supports:
    - RFC3339: "2024-01-01T00:00:00Z"
    - Relative: "1h", "30m", "2h30m"
    - Unix timestamp: "1704067200"
    """
    if not time_str:
        return int(datetime.now(timezone.utc).timestamp() * 1e9)

    # Try relative time (e.g., "1h", "30m")
    if time_str.endswith("h") or time_str.endswith("m") or time_str.endswith("s"):
        try:
            now = datetime.now(timezone.utc)
            if time_str.endswith("h"):
                hours = int(time_str[:-1])
                delta = timedelta(hours=hours)
            elif time_str.endswith("m"):
                minutes = int(time_str[:-1])
                delta = timedelta(minutes=minutes)
            elif time_str.endswith("s"):
                seconds = int(time_str[:-1])
                delta = timedelta(seconds=seconds)
            else:
                delta = timedelta(0)
            
            target_time = now - delta
            return int(target_time.timestamp() * 1e9)
        except ValueError:
            pass

    # Try RFC3339
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1e9)
    except ValueError:
        pass

    # Try Unix timestamp
    try:
        ts = float(time_str)
        return int(ts * 1e9)
    except ValueError:
        pass

    # Default to now
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


@mcp.tool()
def query_logs(
    logql: str,
    limit: int = 100,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> str:
    """
    Query logs from Loki using LogQL syntax.
    
    Args:
        logql: LogQL query string. Streams are selected by label; this
            Loki indexes container, filename, job, level, namespace, pod,
            service and stream. There is NO `app` label - use `service`, e.g.
            '{service="payment-service"} |= "error"'. A selector naming a label
            that does not exist matches nothing and proves nothing.
        limit: Maximum number of log lines (1-1000)
        start_time: Start time (RFC3339, relative like '1h', or unix timestamp)
        end_time: End time (RFC3339, relative, or unix timestamp)
    
    Returns:
        JSON string with query results
    """
    logger.info(f"Querying Loki: {logql}")

    # Parse times
    end_ns = _parse_time(end_time) if end_time else int(
        datetime.now(timezone.utc).timestamp() * 1e9
    )
    start_ns = _parse_time(start_time) if start_time else end_ns - int(
        1 * 3600 * 1e9
    )  # Default: last 1 hour

    # Build Loki query parameters
    bounded_limit = min(max(limit, 1), 1000)
    query_params = {
        "query": logql,
        "start": start_ns,
        "end": end_ns,
        "limit": bounded_limit,
    }

    try:
        response = requests.get(LOKI_QUERY_ENDPOINT, params=query_params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Parse Loki response
        logs = []
        streams_matched = 0
        if data.get("status") == "success" and "data" in data:
            result = data["data"].get("result", [])
            streams_matched = len(result)
            for stream in result:
                if "values" in stream:
                    for value in stream["values"]:
                        # value is [timestamp_ns, log_line]
                        logs.append({
                            "timestamp": value[0],
                            "labels": stream.get("stream", {}),
                            "message": value[1],
                        })

        result = {
            "query": logql,
            "streams_matched": streams_matched,
            **_cap_logs(logs[:bounded_limit], len(logs)),
        }

        # An empty result is ambiguous: a selector naming a label that does not
        # exist returns exactly what a genuinely quiet service returns. Left
        # undistinguished, a typo reads as positive evidence of silence - which
        # is how trial 6's logs lane concluded the service was quiet from five
        # queries that could never have matched. Decide which case this is.
        if not logs:
            result.update(_diagnose_empty_result(logql, start_ns, end_ns))

        return json.dumps(result, separators=(",", ":"))

    except requests.exceptions.RequestException as e:
        logger.error(f"Loki API error: {e}")
        return json.dumps({"error": f"Loki query failed: {str(e)}"})


@mcp.tool()
def get_error_logs(
    app: Optional[str] = None,
    namespace: Optional[str] = None,
    level: str = "error",
    limit: int = 100,
    since: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> str:
    """
    Get error logs filtered by application, namespace, and log level.
    
    Args:
        app: Service name; matched against the `service` label (this Loki has
            no `app` label)
        namespace: Namespace filter
        level: Log level (error, warn, fatal)
        limit: Maximum number of log lines (1-1000)
        since: Relative start time for legacy callers (e.g., '30m')
        start_time: Explicit incident-window start (preferred)
        end_time: Explicit incident-window end (preferred)
    
    Returns:
        JSON string with error logs
    """
    logger.info(f"Getting error logs: app={app}, namespace={namespace}, level={level}")

    # Build LogQL query
    label_filters = []
    if app:
        label_filters.append(f'service="{app}"')
    if namespace:
        label_filters.append(f'namespace="{namespace}"')

    label_query = "{" + ",".join(label_filters) + "}" if label_filters else "{}"
    
    # Add level filter
    level_filter = f'|~ "{level.upper()}"' if level else ""
    
    logql_query = f'{label_query} {level_filter}'

    # Use query_logs with since parameter
    return query_logs(
        logql=logql_query,
        limit=limit,
        start_time=start_time or since or "1h",
        end_time=end_time,
    )


@mcp.tool()
def analyze_log_patterns(
    logql: str,
    pattern: Optional[str] = None,
    limit: int = 1000,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> str:
    """
    Analyze log patterns by querying logs and searching for regex patterns.
    
    Args:
        logql: LogQL query string
        pattern: Regex pattern to search for
        limit: Maximum number of log lines to analyze (1-1000)
        start_time: Explicit incident-window start
        end_time: Explicit incident-window end
    
    Returns:
        JSON string with pattern analysis results
    """
    logger.info(f"Analyzing log patterns: {logql}")

    # Query logs
    logs_result = query_logs(
        logql=logql,
        limit=min(max(limit, 1), 1000),
        start_time=start_time or "1h",
        end_time=end_time,
    )
    
    # Parse logs
    logs_data = json.loads(logs_result)
    if "error" in logs_data:
        return logs_result
    
    logs = logs_data.get("logs", [])

    # Analyze patterns
    pattern_matches = []
    if pattern:
        pattern_re = re.compile(pattern, re.IGNORECASE)
        for log in logs:
            if pattern_re.search(log.get("message", "")):
                pattern_matches.append(log)

    # Count occurrences
    message_counts = {}
    for log in logs:
        msg = log.get("message", "")
        # Extract key parts (first 50 chars)
        key = msg[:50] if len(msg) > 50 else msg
        message_counts[key] = message_counts.get(key, 0) + 1

    # Get top patterns
    top_patterns = sorted(message_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    result = {
        "query": logql,
        "pattern": pattern,
        "total_logs": len(logs),
        "pattern_matches": len(pattern_matches),
        "top_patterns": [{"message": msg, "count": count} for msg, count in top_patterns],
        "sample_matches": pattern_matches[:10] if pattern_matches else [],
    }

    # Carry query_logs' empty-result verdict through. Without this a selector
    # that matched nothing arrives here as a bare total_logs=0 and reads as
    # silence - the same laundering the diagnosis exists to prevent.
    for key in _EMPTY_RESULT_KEYS:
        if key in logs_data:
            result[key] = logs_data[key]

    return json.dumps(result, separators=(",", ":"))


if __name__ == "__main__":
    logger.info(f"Starting Loki MCP Server on {host}:{port}")
    from mcp_auth import run_authenticated_sse
    run_authenticated_sse(mcp, host=host, port=port)
