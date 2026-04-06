#!/usr/bin/env python3
"""
Inventory Service — tracks product stock levels.

Generates incidents:
  - Random slow DB queries (simulate index miss)
  - Occasional 404s for unknown items
  - Stock-out warnings logged to Loki
  - CPU spike simulation on /reindex
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
            "service": "inventory-service",
            "message": record.getMessage(),
            **({**{"exception": self.formatException(record.exc_info)}} if record.exc_info else {}),
        })

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("inventory")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Prometheus metrics ───────────────────────────────────────────────────────
REQUEST_COUNT   = Counter("http_requests_total",             "Total requests",    ["service", "method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Request latency",   ["service", "endpoint"],
                            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
ERROR_COUNT     = Counter("http_errors_total",               "Total errors",      ["service", "endpoint", "error_type"])
STOCK_LEVEL     = Gauge("inventory_stock_level",             "Current stock",     ["item_id"])
DB_QUERY_TIME   = Histogram("db_query_duration_seconds",     "DB query latency",  ["query"])

# ── Mutable runtime config ──────────────────────────────────────────────────
config = {
    "slow_query_rate": float(os.getenv("SLOW_QUERY_RATE", "0.25")),
}

# ── Fake inventory data ──────────────────────────────────────────────────────
ITEMS = {
    "item-001": {"name": "Widget Pro",      "stock": 142, "price": 29.99},
    "item-002": {"name": "Gadget Plus",     "stock": 3,   "price": 99.99},   # near stock-out
    "item-003": {"name": "Doohickey Max",   "stock": 0,   "price": 49.99},   # stock-out
    "item-004": {"name": "Thingamajig",     "stock": 87,  "price": 14.99},
    "item-005": {"name": "Whatchamacallit", "stock": 12,  "price": 199.99},
}

# Expose initial stock as Gauge metrics
for item_id, item in ITEMS.items():
    STOCK_LEVEL.labels(item_id=item_id).set(item["stock"])

app = FastAPI(title="inventory-service")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Admin config endpoints ───────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    slow_query_rate: Optional[float] = None

@app.get("/admin/config")
def get_config():
    return config

@app.post("/admin/config")
def set_config(update: ConfigUpdate):
    if update.slow_query_rate is not None:
        config["slow_query_rate"] = max(0.0, min(1.0, update.slow_query_rate))
    logger.info(f"Config updated: {config}")
    return config

async def simulate_db_query(query_name: str):
    """Simulate database query with occasional slowness."""
    if random.random() < config["slow_query_rate"]:
        delay = random.uniform(0.8, 2.5)
        logger.warning(f"Slow DB query detected query={query_name} delay_seconds={delay:.2f}")
        DB_QUERY_TIME.labels(query=query_name).observe(delay)
        await asyncio.sleep(delay)
    else:
        delay = random.uniform(0.001, 0.02)
        DB_QUERY_TIME.labels(query=query_name).observe(delay)
        await asyncio.sleep(delay)

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "inventory-service"}

@app.get("/items")
async def list_items():
    start = time.time()
    await simulate_db_query("list_all_items")

    # Log stock-out warnings
    for item_id, item in ITEMS.items():
        if item["stock"] == 0:
            logger.warning(f"Stock out detected item_id={item_id} name={item['name']}")
        elif item["stock"] < 5:
            logger.warning(f"Low stock alert item_id={item_id} name={item['name']} remaining={item['stock']}")

    duration = time.time() - start
    REQUEST_LATENCY.labels(service="inventory-service", endpoint="/items").observe(duration)
    REQUEST_COUNT.labels(service="inventory-service", method="GET", endpoint="/items", status="200").inc()
    logger.info(f"Inventory list served duration_ms={round(duration * 1000)} item_count={len(ITEMS)}")
    return {"items": ITEMS, "total": len(ITEMS)}

@app.get("/items/{item_id}")
async def get_item(item_id: str):
    start = time.time()
    await simulate_db_query(f"get_item_{item_id}")

    if item_id not in ITEMS:
        ERROR_COUNT.labels(service="inventory-service", endpoint="/items/{item_id}", error_type="not_found").inc()
        REQUEST_COUNT.labels(service="inventory-service", method="GET", endpoint="/items/{item_id}", status="404").inc()
        logger.warning(f"Item not found item_id={item_id}")
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

    item = ITEMS[item_id]
    duration = time.time() - start
    REQUEST_LATENCY.labels(service="inventory-service", endpoint="/items/{item_id}").observe(duration)
    REQUEST_COUNT.labels(service="inventory-service", method="GET", endpoint="/items/{item_id}", status="200").inc()
    logger.info(f"Item fetched item_id={item_id} stock={item['stock']} duration_ms={round(duration * 1000)}")
    return item

@app.post("/reindex")
async def reindex():
    """CPU/memory intensive operation — triggers resource alerts."""
    logger.info("Starting inventory reindex operation")
    start = time.time()
    # Simulate heavy computation
    result = sum(i * i for i in range(1_000_000))
    duration = time.time() - start
    REQUEST_COUNT.labels(service="inventory-service", method="POST", endpoint="/reindex", status="200").inc()
    logger.info(f"Reindex complete duration_ms={round(duration * 1000)} checksum={result % 9999}")
    return {"status": "reindexed", "duration_ms": round(duration * 1000)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
