#!/usr/bin/env python3
"""
API Gateway — entry point for all client traffic.
Routes to checkout-service and inventory-service.
Emits Prometheus metrics and structured JSON logs.
"""

import json
import logging
import os
import time
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# ── Structured JSON logger ──────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "api-gateway",
            "message": record.getMessage(),
            **({"exception": self.formatException(record.exc_info)} if record.exc_info else {}),
        })

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("api-gateway")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Prometheus metrics ───────────────────────────────────────────────────────
REQUEST_COUNT   = Counter("http_requests_total",            "Total HTTP requests",       ["service", "method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds","Request latency (seconds)", ["service", "endpoint"],
                            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
ACTIVE_REQUESTS = Gauge("http_active_requests",             "In-flight requests",        ["service"])
ERROR_COUNT     = Counter("http_errors_total",              "Total HTTP errors",         ["service", "endpoint", "error_type"])

CHECKOUT_URL  = os.getenv("CHECKOUT_SERVICE_URL",  "http://checkout-service:8001")
INVENTORY_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory-service:8002")

app = FastAPI(title="api-gateway")

# ── Middleware: record latency + active requests ─────────────────────────────
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    ACTIVE_REQUESTS.labels(service="api-gateway").inc()
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    REQUEST_LATENCY.labels(service="api-gateway", endpoint=request.url.path).observe(duration)
    REQUEST_COUNT.labels(
        service="api-gateway", method=request.method,
        endpoint=request.url.path, status=str(response.status_code)
    ).inc()
    ACTIVE_REQUESTS.labels(service="api-gateway").dec()
    return response

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway"}

@app.post("/checkout/{order_id}")
async def checkout(order_id: str):
    logger.info(f"Routing checkout request for order={order_id}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{CHECKOUT_URL}/process", params={"order_id": order_id})
        if resp.status_code >= 500:
            ERROR_COUNT.labels(service="api-gateway", endpoint="/checkout", error_type="upstream_error").inc()
            logger.error(f"Checkout upstream error order={order_id} status={resp.status_code} body={resp.text}")
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.TimeoutException:
        ERROR_COUNT.labels(service="api-gateway", endpoint="/checkout", error_type="timeout").inc()
        logger.error(f"Checkout timeout for order={order_id}")
        return JSONResponse(status_code=504, content={"error": "checkout service timeout"})
    except Exception as e:
        ERROR_COUNT.labels(service="api-gateway", endpoint="/checkout", error_type="connection_error").inc()
        logger.error(f"Checkout connection error order={order_id} error={str(e)}")
        return JSONResponse(status_code=503, content={"error": "checkout service unavailable"})

@app.get("/inventory")
async def get_inventory():
    logger.info("Routing inventory list request")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{INVENTORY_URL}/items")
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.TimeoutException:
        ERROR_COUNT.labels(service="api-gateway", endpoint="/inventory", error_type="timeout").inc()
        logger.error("Inventory service timeout")
        return JSONResponse(status_code=504, content={"error": "inventory service timeout"})
    except Exception as e:
        ERROR_COUNT.labels(service="api-gateway", endpoint="/inventory", error_type="connection_error").inc()
        logger.error(f"Inventory connection error: {e}")
        return JSONResponse(status_code=503, content={"error": "inventory service unavailable"})

@app.get("/inventory/{item_id}")
async def get_item(item_id: str):
    logger.info(f"Routing inventory lookup item={item_id}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{INVENTORY_URL}/items/{item_id}")
        if resp.status_code == 404:
            logger.warning(f"Item not found item={item_id}")
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        ERROR_COUNT.labels(service="api-gateway", endpoint="/inventory/{item_id}", error_type="connection_error").inc()
        logger.error(f"Inventory lookup error item={item_id} error={e}")
        return JSONResponse(status_code=503, content={"error": "inventory service unavailable"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
