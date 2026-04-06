#!/usr/bin/env python3
"""
Checkout Service — processes orders and handles payments.

Intentionally flaky to generate realistic incidents:
  - 15% of requests fail with payment gateway errors
  - 20% of requests are slow (1.5–3s)
  - Memory grows slightly over time (simulate leak)
  - DB connection errors spike when CHAOS_MODE=true
"""

import asyncio
import json
import logging
import os
import random
import time
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# ── Structured JSON logger ──────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "checkout-service",
            "message": record.getMessage(),
            **({**{"exception": self.formatException(record.exc_info)}} if record.exc_info else {}),
        })

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("checkout")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Prometheus metrics ───────────────────────────────────────────────────────
REQUEST_COUNT    = Counter("http_requests_total",             "Total requests",        ["service", "method", "endpoint", "status"])
REQUEST_LATENCY  = Histogram("http_request_duration_seconds", "Request latency",       ["service", "endpoint"],
                             buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
ERROR_COUNT      = Counter("http_errors_total",               "Total errors",          ["service", "endpoint", "error_type"])
PAYMENT_FAILURES = Counter("payment_failures_total",          "Payment failures",      ["reason"])
MEMORY_BYTES     = Gauge("process_memory_bytes_simulated",    "Simulated memory usage", ["service"])

# Simulate slow memory growth
_leak_store: list = []

# ── Mutable runtime config ──────────────────────────────────────────────────
config = {
    "chaos_mode": os.getenv("CHAOS_MODE", "false").lower() == "true",
    "error_rate": float(os.getenv("ERROR_RATE", "0.15")),
    "slow_rate":  float(os.getenv("SLOW_RATE",  "0.20")),
}

app = FastAPI(title="checkout-service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Admin config endpoints ───────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    error_rate: Optional[float] = None
    slow_rate: Optional[float] = None
    chaos_mode: Optional[bool] = None

@app.get("/admin/config")
def get_config():
    return config

@app.post("/admin/config")
def set_config(update: ConfigUpdate):
    if update.error_rate is not None:
        config["error_rate"] = max(0.0, min(1.0, update.error_rate))
    if update.slow_rate is not None:
        config["slow_rate"] = max(0.0, min(1.0, update.slow_rate))
    if update.chaos_mode is not None:
        config["chaos_mode"] = update.chaos_mode
    logger.info(f"Config updated: {config}")
    return config

# ── Service endpoints ────────────────────────────────────────────────────────
@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "checkout-service", "chaos_mode": config["chaos_mode"]}

@app.post("/process")
async def process_checkout(order_id: str = "unknown"):
    start = time.time()

    # Simulate memory leak (tiny allocation per request)
    _leak_store.append(b"x" * 1024)
    MEMORY_BYTES.labels(service="checkout-service").set(len(_leak_store) * 1024)

    # Chaos mode: DB connection errors (higher failure rate)
    if config["chaos_mode"] and random.random() < 0.50:
        reason = random.choice(["db_connection_refused", "db_timeout", "db_pool_exhausted"])
        ERROR_COUNT.labels(service="checkout-service", endpoint="/process", error_type=reason).inc()
        PAYMENT_FAILURES.labels(reason=reason).inc()
        REQUEST_COUNT.labels(service="checkout-service", method="POST", endpoint="/process", status="500").inc()
        logger.error(f"Database error processing order={order_id} reason={reason} chaos=true")
        raise HTTPException(status_code=500, detail=f"Database error: {reason}")

    # Normal mode: payment gateway failures
    if random.random() < config["error_rate"]:
        reason = random.choice([
            "payment_gateway_timeout",
            "card_declined",
            "fraud_detected",
            "gateway_unavailable",
        ])
        ERROR_COUNT.labels(service="checkout-service", endpoint="/process", error_type=reason).inc()
        PAYMENT_FAILURES.labels(reason=reason).inc()
        REQUEST_COUNT.labels(service="checkout-service", method="POST", endpoint="/process", status="500").inc()
        logger.error(f"Payment failed order={order_id} reason={reason}")
        raise HTTPException(status_code=500, detail=f"Payment failed: {reason}")

    # Slow processing
    if random.random() < config["slow_rate"]:
        delay = random.uniform(1.5, 3.5)
        logger.warning(f"Slow payment processing order={order_id} delay_seconds={delay:.2f}")
        await asyncio.sleep(delay)

    duration = time.time() - start
    REQUEST_LATENCY.labels(service="checkout-service", endpoint="/process").observe(duration)
    REQUEST_COUNT.labels(service="checkout-service", method="POST", endpoint="/process", status="200").inc()
    logger.info(f"Order processed successfully order={order_id} duration_ms={round(duration * 1000)}")
    return {"order_id": order_id, "status": "processed", "duration_ms": round(duration * 1000)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
