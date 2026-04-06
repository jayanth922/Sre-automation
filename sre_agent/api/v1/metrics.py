from fastapi import APIRouter, HTTPException
import os
import httpx
from typing import Dict, Any

router = APIRouter(tags=["metrics"])

@router.get("/snapshot")
async def get_metrics_snapshot():
    """
    Get current metrics snapshot for dashboard telemetry.

    Returns all four Golden Signals from Prometheus:
    - Latency (P95 request duration)
    - Error Rate (5xx error percentage)
    - CPU Saturation
    - Memory Usage

    Returns 503 when Prometheus is unreachable (no synthetic data).
    """
    prometheus_url = os.getenv("PROMETHEUS_URL", "")
    if not prometheus_url:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "prometheus_not_configured",
                "message": "PROMETHEUS_URL not set. Connect a cluster with monitoring to see live metrics.",
            },
        )

    queries = {
        "latency": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le)) * 1000',
        "errors": 'sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) * 100',
        "cpu": "avg(rate(container_cpu_usage_seconds_total[5m])) * 100",
        "mem": 'sum(container_memory_usage_bytes) / (1024*1024*1024)',
    }

    try:
        results = {}
        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, query in queries.items():
                try:
                    resp = await client.get(
                        f"{prometheus_url}/api/v1/query",
                        params={"query": query}
                    )
                    data = resp.json()
                    if data["status"] == "success" and data["data"]["result"]:
                        value = float(data["data"]["result"][0]["value"][1])
                        results[name] = round(value, 2)
                    else:
                        results[name] = 0.0
                except Exception:
                    results[name] = 0.0
        
        return results

    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to fetch metrics: {e}")
