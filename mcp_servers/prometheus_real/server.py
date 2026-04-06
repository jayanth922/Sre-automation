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
from typing import Any, Dict, List, Optional

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
        }, indent=2)
    else:
        return json.dumps({
            "status": "unhealthy",
            "url": url,
            "message": "Failed to connect to Prometheus. Check PROMETHEUS_URL and network connectivity."
        }, indent=2)

@mcp.tool()
async def get_metric(query: str, time: str = None) -> str:
    """
    Query a Prometheus metric using PromQL. Returns the current value or value at specified time.
    
    Args:
        query: PromQL query string (e.g., 'cpu_usage{namespace="production"}')
        time: RFC3339 timestamp or unix timestamp (optional, defaults to now)
    """
    client = get_prom_client()
    if not client:
        return "Error: Could not connect to Prometheus. Please check infrastructure status."

    logger.info(f"Querying Prometheus: {query}")

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        if time:
            result = await loop.run_in_executor(
                None, client.custom_query, query, time
            )
        else:
            result = await loop.run_in_executor(None, client.custom_query, query)
        
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error querying metric: {e}")
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

    logger.info(
        f"Querying Prometheus range: {query} from {start_time} to {end_time}"
    )

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            client.custom_query_range,
            query,
            start_time,
            end_time,
            step,
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error querying metric range: {e}")
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
        time: Time for query (optional)
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
    for signal_name, query in queries.items():
        try:
            if time:
                result = await loop.run_in_executor(
                    None, client.custom_query, query, time
                )
            else:
                result = await loop.run_in_executor(None, client.custom_query, query)
            results[signal_name] = {
                "query": query,
                "value": result,
            }
        except Exception as e:
            logger.warning(f"Failed to query {signal_name}: {e}")
            results[signal_name] = {
                "query": query,
                "error": str(e),
            }

    return json.dumps(results, indent=2, default=str)


if __name__ == "__main__":
    logger.info("Starting FastMCP server execution...")
    
    # Try initial connection non-blocking intended, but get_prom_client is sync for now
    # We can try once at startup to warm up
    get_prom_client()
    
    mcp.run(transport="sse")
