#!/usr/bin/env python3
"""
Real Loki MCP Server (Native FastMCP)

This MCP server directly queries Grafana Loki using the HTTP API.
Uses standard mcp.server.fastmcp implementation.
"""

import json
import logging
import os
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

# Initialize FastMCP server
port = int(os.getenv("HTTP_PORT", "3000"))
host = os.getenv("HOST", "0.0.0.0")

mcp = FastMCP("Loki Logs", host=host, port=port)


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
        logql: LogQL query string (e.g., '{app="payment"} |= "error"')
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
    query_params = {
        "query": logql,
        "start": start_ns,
        "end": end_ns,
        "limit": min(max(limit, 1), 1000),  # Clamp between 1 and 1000
    }

    try:
        response = requests.get(LOKI_QUERY_ENDPOINT, params=query_params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Parse Loki response
        logs = []
        if data.get("status") == "success" and "data" in data:
            result = data["data"].get("result", [])
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
            "logs": logs[:limit],
            "count": len(logs),
        }

        return json.dumps(result, indent=2)

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
) -> str:
    """
    Get error logs filtered by application, namespace, and log level.
    
    Args:
        app: Application name filter
        namespace: Namespace filter
        level: Log level (error, warn, fatal)
        limit: Maximum number of log lines (1-1000)
        since: Time range (e.g., '1h', '30m')
    
    Returns:
        JSON string with error logs
    """
    logger.info(f"Getting error logs: app={app}, namespace={namespace}, level={level}")

    # Build LogQL query
    label_filters = []
    if app:
        label_filters.append(f'app="{app}"')
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
        start_time=since if since else "1h",
    )


@mcp.tool()
def analyze_log_patterns(
    logql: str,
    pattern: Optional[str] = None,
    limit: int = 1000,
) -> str:
    """
    Analyze log patterns by querying logs and searching for regex patterns.
    
    Args:
        logql: LogQL query string
        pattern: Regex pattern to search for
        limit: Maximum number of log lines to analyze (1-5000)
    
    Returns:
        JSON string with pattern analysis results
    """
    logger.info(f"Analyzing log patterns: {logql}")

    # Query logs
    logs_result = query_logs(logql=logql, limit=min(max(limit, 1), 5000), start_time="1h")
    
    # Parse logs
    logs_data = json.loads(logs_result)
    if "error" in logs_data:
        return logs_result
    
    logs = logs_data.get("logs", [])

    # Analyze patterns
    pattern_matches = []
    if pattern:
        import re
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

    return json.dumps(result, indent=2)


if __name__ == "__main__":
    logger.info(f"Starting Loki MCP Server on {host}:{port}")
    mcp.run(transport="sse")
