#!/usr/bin/env python3
"""
Load Generator — continuously fires traffic at the demo services.

Simulates realistic user behavior:
  - Mix of checkout and inventory requests
  - Burst mode every ~60s to spike error rates
  - Random order IDs to vary logs
  - Admin API on port 8003 for runtime config changes
"""

import asyncio
import json
import logging
import os
import random
import string
import time
import threading
import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://api-gateway:8000")

config = {
    "rps": float(os.getenv("RPS", "5")),
    "burst_rps": float(os.getenv("BURST_RPS", "20")),
    "burst_duration": int(os.getenv("BURST_DURATION", "15")),
    "burst_interval": int(os.getenv("BURST_INTERVAL", "60")),
}

# Manual burst trigger
manual_burst = {"active": False}

logging.basicConfig(
    level=logging.INFO,
    format=json.dumps({
        "timestamp": "%(asctime)s", "level": "%(levelname)s",
        "service": "load-generator", "message": "%(message)s"
    })
)
logger = logging.getLogger("load-gen")

ITEM_IDS = ["item-001", "item-002", "item-003", "item-004", "item-005", "item-999"]

def random_order_id() -> str:
    return "ord-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

# ── Admin API (port 8003) ──────────────────────────────────────────────────
admin_app = FastAPI(title="load-generator-admin")
admin_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ConfigUpdate(BaseModel):
    rps: Optional[float] = None
    burst_rps: Optional[float] = None
    burst_duration: Optional[int] = None
    burst_interval: Optional[int] = None

@admin_app.get("/admin/config")
def get_config():
    return config

@admin_app.post("/admin/config")
def set_config(update: ConfigUpdate):
    if update.rps is not None:
        config["rps"] = max(1.0, min(100.0, update.rps))
    if update.burst_rps is not None:
        config["burst_rps"] = max(1.0, min(100.0, update.burst_rps))
    if update.burst_duration is not None:
        config["burst_duration"] = max(1, min(120, update.burst_duration))
    if update.burst_interval is not None:
        config["burst_interval"] = max(10, min(600, update.burst_interval))
    logger.info(f"Config updated: {config}")
    return config

@admin_app.post("/admin/trigger-burst")
def trigger_burst():
    manual_burst["active"] = True
    logger.warning("Manual burst triggered!")
    return {"status": "burst_triggered", "burst_rps": config["burst_rps"], "burst_duration": config["burst_duration"]}

@admin_app.get("/health")
def health():
    return {"status": "ok", "service": "load-generator"}

# ── Load generation logic ──────────────────────────────────────────────────
async def checkout(client: httpx.AsyncClient):
    order_id = random_order_id()
    try:
        resp = await client.post(f"{GATEWAY_URL}/checkout/{order_id}", timeout=6.0)
        status = resp.status_code
        if status >= 500:
            logger.warning(f"Checkout failed order={order_id} status={status}")
        else:
            logger.info(f"Checkout ok order={order_id} status={status}")
    except Exception as e:
        logger.error(f"Checkout error order={order_id} error={e}")

async def inventory_list(client: httpx.AsyncClient):
    try:
        resp = await client.get(f"{GATEWAY_URL}/inventory", timeout=6.0)
        logger.info(f"Inventory list status={resp.status_code}")
    except Exception as e:
        logger.error(f"Inventory list error={e}")

async def inventory_item(client: httpx.AsyncClient):
    item_id = random.choice(ITEM_IDS)
    try:
        resp = await client.get(f"{GATEWAY_URL}/inventory/{item_id}", timeout=6.0)
        if resp.status_code == 404:
            logger.warning(f"Inventory 404 item={item_id}")
        else:
            logger.info(f"Inventory item ok item={item_id} status={resp.status_code}")
    except Exception as e:
        logger.error(f"Inventory item error item={item_id} error={e}")

async def reindex(client: httpx.AsyncClient):
    try:
        await client.post(f"{GATEWAY_URL.replace('8000', '8002')}/reindex", timeout=10.0)
        logger.info("Reindex triggered")
    except Exception:
        pass

async def run_request(client: httpx.AsyncClient):
    roll = random.random()
    if roll < 0.45:
        await checkout(client)
    elif roll < 0.75:
        await inventory_list(client)
    elif roll < 0.95:
        await inventory_item(client)
    else:
        await reindex(client)

async def main():
    logger.info(f"Load generator starting — target={GATEWAY_URL} rps={config['rps']} burst_rps={config['burst_rps']}")

    await asyncio.sleep(5)

    burst_active = False
    last_burst_end = 0.0

    limits = httpx.Limits(max_keepalive_connections=500, max_connections=1000)
    async with httpx.AsyncClient(limits=limits) as client:
        while True:
            loop_start = time.time()

            # Check for manual burst trigger
            if manual_burst["active"]:
                manual_burst["active"] = False
                burst_active = True
                burst_end = loop_start + config["burst_duration"]
                logger.warning(f"Manual burst ON for {config['burst_duration']}s at {config['burst_rps']} rps")

            # End burst mode if duration has expired
            if burst_active and loop_start >= burst_end:
                burst_active = False
                logger.info("Burst mode OFF")

            current_rps = config["burst_rps"] if burst_active else config["rps"]
            
            # Fire and forget tasks so slow responses don't block the next second's traffic
            for _ in range(int(current_rps)):
                asyncio.create_task(run_request(client))
            
            # Sleep precisely the remainder of 1 second
            elapsed = time.time() - loop_start
            sleep_time = max(0.0, 1.0 - elapsed)
            await asyncio.sleep(sleep_time)

def run_admin_server():
    uvicorn.run(admin_app, host="0.0.0.0", port=8003, log_level="warning")

if __name__ == "__main__":
    # Start admin API in a background thread
    admin_thread = threading.Thread(target=run_admin_server, daemon=True)
    admin_thread.start()
    # Run load generator in the main event loop
    asyncio.run(main())
