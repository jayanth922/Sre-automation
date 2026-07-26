#!/usr/bin/env python3
"""
Payment Service — a downstream dependency of checkout-service.

Adds a realistic *dependency chain* (checkout → payment) so the platform can
observe cascading failures: when this provider degrades or goes down, checkout's
error rate spikes even though checkout itself is healthy. This surfaces the
"downstream dependency failure" incident class.

Chaos knobs (env or POST /admin/config):
  - error_rate:    fraction of charges that fail (default 0.05)
  - slow_rate:     fraction of charges that are slow (default 0.10)
  - provider_down: hard outage — every charge 503s, payment_provider_up=0
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


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "payment-service",
            "message": record.getMessage(),
            **({**{"exception": self.formatException(record.exc_info)}} if record.exc_info else {}),
        })


handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("payment")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

REQUEST_COUNT   = Counter("http_requests_total",             "Total requests",   ["service", "method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Request latency",  ["service", "endpoint"],
                            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
ERROR_COUNT     = Counter("http_errors_total",               "Total errors",     ["service", "endpoint", "error_type"])
PAYMENT_FAILURES = Counter("payment_failures_total",         "Payment failures", ["reason"])
PROVIDER_UP     = Gauge("payment_provider_up",               "1 if the payment provider is up, else 0", ["service"])

config = {
    "error_rate":    float(os.getenv("ERROR_RATE", "0.05")),
    "slow_rate":     float(os.getenv("SLOW_RATE", "0.10")),
    "provider_down": os.getenv("PROVIDER_DOWN", "false").lower() == "true",
}

app = FastAPI(title="payment-service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
PROVIDER_UP.labels(service="payment-service").set(0 if config["provider_down"] else 1)


class ConfigUpdate(BaseModel):
    error_rate: Optional[float] = None
    slow_rate: Optional[float] = None
    provider_down: Optional[bool] = None


@app.get("/admin/config")
def get_config():
    return config


@app.post("/admin/config")
def set_config(update: ConfigUpdate):
    if update.error_rate is not None:
        config["error_rate"] = max(0.0, min(1.0, update.error_rate))
    if update.slow_rate is not None:
        config["slow_rate"] = max(0.0, min(1.0, update.slow_rate))
    if update.provider_down is not None:
        config["provider_down"] = update.provider_down
        PROVIDER_UP.labels(service="payment-service").set(0 if update.provider_down else 1)
    logger.info(f"Config updated: {config}")
    return config


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service", "provider_down": config["provider_down"]}


@app.post("/charge")
async def charge(order_id: str = "unknown", amount: float = 0.0):
    start = time.time()

    # Hard outage: the whole provider is down.
    if config["provider_down"]:
        ERROR_COUNT.labels(service="payment-service", endpoint="/charge", error_type="provider_down").inc()
        PAYMENT_FAILURES.labels(reason="provider_down").inc()
        REQUEST_COUNT.labels(service="payment-service", method="POST", endpoint="/charge", status="503").inc()
        logger.error(f"Payment provider DOWN order={order_id}")
        raise HTTPException(status_code=503, detail="Payment provider unavailable")

    # Transient failures.
    if random.random() < config["error_rate"]:
        reason = random.choice(["card_declined", "insufficient_funds", "provider_timeout"])
        ERROR_COUNT.labels(service="payment-service", endpoint="/charge", error_type=reason).inc()
        PAYMENT_FAILURES.labels(reason=reason).inc()
        REQUEST_COUNT.labels(service="payment-service", method="POST", endpoint="/charge", status="502").inc()
        logger.error(f"Charge failed order={order_id} reason={reason}")
        raise HTTPException(status_code=502, detail=f"Charge failed: {reason}")

    if random.random() < config["slow_rate"]:
        delay = random.uniform(1.0, 3.0)
        logger.warning(f"Slow charge order={order_id} delay_seconds={delay:.2f}")
        await asyncio.sleep(delay)

    duration = time.time() - start
    REQUEST_LATENCY.labels(service="payment-service", endpoint="/charge").observe(duration)
    REQUEST_COUNT.labels(service="payment-service", method="POST", endpoint="/charge", status="200").inc()
    logger.info(f"Charge ok order={order_id} amount={amount} duration_ms={round(duration * 1000)}")
    return {"order_id": order_id, "status": "charged", "amount": amount}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
