#!/usr/bin/env python3
"""
MTTR Benchmark — Multi-Agent SRE System
========================================
Measures Mean Time To Resolution (MTTR = resolved_at - created_at) across
multiple alert scenarios and repeated runs (pass^k consistency).

How it works
------------
1. POST a synthetic Alertmanager webhook payload to the FastAPI backend.
2. Poll the incidents API until the incident reaches RESOLVED status.
3. Compute MTTR from the timestamps already stored in PostgreSQL.
4. Repeat each scenario RUNS_PER_SCENARIO times, then report statistics.

No code changes to the application are required — both timestamps
(incident.created_at and incident.resolved_at) are written by the
existing pipeline.

Prerequisites
-------------
- Platform stack running:  cd platform && docker compose up -d
- Python deps available:   uv sync  (httpx is already in pyproject.toml)

Run
---
    cd /path/to/Multi-Agent-SRE-System
    uv run python benchmarks/bench_mttr.py

Comparison baseline
-------------------
- Rootly industry benchmark (AI-assisted):  < 90 s  MTTR from alert to root cause
- Human SRE average:                        ~28 min
- Flow-of-Action GPT-4 (WWW'25):            not reported (accuracy-only)
"""

import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from typing import Optional

import httpx

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL          = "http://localhost:8080"
ADMIN_EMAIL       = "admin@example.com"
ADMIN_PASSWORD    = "admin"
CLUSTER_ID        = "df4ab154-2b84-4570-93c6-9c9a70ef9baf"
CLUSTER_TOKEN     = "cl_438450df3cb94ea78760f4e005088c2a"

RUNS_PER_SCENARIO = 3     # pass^k — 3 runs per scenario
                          # NVIDIA NIM (llama-3.3-70b-instruct): generous free credits
POLL_INTERVAL_SEC = 5     # how often to check incident status
TIMEOUT_SEC       = 300   # max wait per run — agents make real MCP calls (Prometheus/Loki/K8s)
COOLDOWN_SEC      = 30    # pause between runs so dedup window clears

# ── Alert scenarios ─────────────────────────────────────────────────────────────
# Each scenario maps to a real Prometheus alert rule in the project.
# The 'service' field is the ground truth for future AC@1 scoring.
SCENARIOS: dict[str, dict] = {
    "checkout_high_error_rate": {
        "alertname":   "CheckoutHighErrorRate",
        "severity":    "critical",
        "service":     "checkout-service",
        "summary":     "43.9% error rate on checkout-service in the last 5 minutes",
        "description": "Error rate exceeded 30% threshold. Possible causes: bad deploy, downstream dependency failure.",
    },
    "checkout_high_latency": {
        "alertname":   "CheckoutHighLatency",
        "severity":    "warning",
        "service":     "checkout-service",
        "summary":     "P95 latency 4.2s on checkout-service, breaching the 2s SLO",
        "description": "Latency spike detected. Possible causes: slow downstream service, database contention.",
    },
    "payment_failure_spike": {
        "alertname":   "PaymentFailureSpike",
        "severity":    "critical",
        "service":     "checkout-service",
        "summary":     "Payment failure rate 0.8/s sustained over the last 5 minutes",
        "description": "Payment failures spiking. Possible causes: payment provider outage, network issues.",
    },
    "inventory_slow_queries": {
        "alertname":   "InventorySlowQueries",
        "severity":    "warning",
        "service":     "inventory-service",
        "summary":     "95th percentile query latency 3.1s on inventory-service",
        "description": "Slow queries detected. Possible causes: missing index, table lock, high traffic load.",
    },
}

# ── Helpers ─────────────────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp from the API into a timezone-aware datetime."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def _fmt(seconds: float) -> str:
    return f"{seconds:.1f}s"


async def _login(client: httpx.AsyncClient) -> str:
    """Login and return a JWT access token."""
    r = await client.post(
        f"{BASE_URL}/auth/token",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def _get_incident_ids(client: httpx.AsyncClient, jwt: str) -> set[str]:
    """Return the set of all current incident IDs for this cluster."""
    r = await client.get(
        f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r.raise_for_status()
    return {inc["id"] for inc in r.json()}


async def _fire_alert(client: httpx.AsyncClient, scenario: dict) -> None:
    """POST a synthetic Alertmanager webhook payload to trigger an incident."""
    payload = {
        "version": "4",
        "status":  "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": scenario["alertname"],
                "severity":  scenario["severity"],
                "service":   scenario["service"],
            },
            "annotations": {
                "summary":     scenario["summary"],
                "description": scenario["description"],
            },
            "startsAt": datetime.now(timezone.utc).isoformat(),
        }],
    }
    r = await client.post(
        f"{BASE_URL}/api/v1/alerts/webhook",
        json=payload,
        headers={"Authorization": f"Bearer {CLUSTER_TOKEN}"},
    )
    r.raise_for_status()
    resp = r.json()
    if resp.get("incidents_created", 0) == 0:
        raise RuntimeError(
            f"Webhook accepted but no incident created (dedup?) — "
            f"response: {resp}"
        )


async def _wait_for_new_incident(
    client: httpx.AsyncClient, jwt: str, known_ids: set[str]
) -> Optional[dict]:
    """Poll until a new incident appears and return it."""
    for _ in range(10):
        await asyncio.sleep(2)
        r = await client.get(
            f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        for inc in r.json():
            if inc["id"] not in known_ids:
                return inc
    return None


async def _wait_for_resolved(
    client: httpx.AsyncClient, jwt: str, incident_id: str
) -> tuple[Optional[dict], str]:
    """
    Poll until the incident reaches RESOLVED or fails.

    Returns (incident_dict, reason) where reason is one of:
      "resolved"  — success
      "failed"    — pipeline set status back to open (e.g. LLM 429 rate limit)
      "timeout"   — exceeded TIMEOUT_SEC without a terminal status
    """
    elapsed = 0
    while elapsed < TIMEOUT_SEC:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC
        r = await client.get(
            f"{BASE_URL}/api/v1/clusters/{CLUSTER_ID}/incidents",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        for inc in r.json():
            if inc["id"] != incident_id:
                continue
            if inc["status"] == "resolved":
                return inc, "resolved"
            # Pipeline failed — agent_runtime_tasks sets status back to OPEN on error
            if inc["status"] == "open" and inc.get("summary", ""):
                return inc, "failed"
    return None, "timeout"


# ── Core benchmark loop ─────────────────────────────────────────────────────────

async def run_benchmark() -> None:
    results: dict[str, list[float]] = {name: [] for name in SCENARIOS}
    total_runs = len(SCENARIOS) * RUNS_PER_SCENARIO

    print("=" * 62)
    print("  MTTR Benchmark — Multi-Agent SRE System")
    print(f"  {len(SCENARIOS)} scenarios × {RUNS_PER_SCENARIO} runs = {total_runs} total cases")
    print("=" * 62)

    async with httpx.AsyncClient(timeout=30) as client:
        jwt = await _login(client)
        print(f"  Logged in as {ADMIN_EMAIL}\n")

        run_number = 0
        for scenario_name, scenario in SCENARIOS.items():
            print(f"── Scenario: {scenario_name}")

            for k in range(1, RUNS_PER_SCENARIO + 1):
                run_number += 1
                print(f"   run {k}/{RUNS_PER_SCENARIO}  ", end="", flush=True)

                # Snapshot existing incidents before firing
                known_ids = await _get_incident_ids(client, jwt)

                # Fire the alert
                try:
                    await _fire_alert(client, scenario)
                except Exception as e:
                    print(f"SKIP (webhook error: {e})")
                    continue

                # Wait for the new incident to appear
                incident = await _wait_for_new_incident(client, jwt, known_ids)
                if incident is None:
                    print("SKIP (incident not created within 20s)")
                    continue

                incident_id = incident["id"]
                created_at  = _parse_iso(incident["created_at"])

                # Wait for resolution
                resolved_incident, reason = await _wait_for_resolved(client, jwt, incident_id)
                if reason == "timeout":
                    print(f"TIMEOUT (>{TIMEOUT_SEC}s)")
                    continue
                if reason == "failed":
                    summary = (resolved_incident.get("summary") or "")[:80]
                    print(f"FAILED  ({summary})")
                    continue

                resolved_at = _parse_iso(resolved_incident["resolved_at"])
                mttr = (resolved_at - created_at).total_seconds()
                results[scenario_name].append(mttr)

                print(f"MTTR = {_fmt(mttr)}")

                # Cooldown between runs
                if k < RUNS_PER_SCENARIO:
                    await asyncio.sleep(COOLDOWN_SEC)

            print()

    # ── Report ─────────────────────────────────────────────────────────────────
    print("=" * 62)
    print("  RESULTS")
    print("=" * 62)
    print(f"  {'Scenario':<32} {'Mean':>7} {'Median':>7} {'p95':>7} {'Min':>7} {'Max':>7}  Runs")
    print(f"  {'-'*32} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}  ----")

    all_mttr: list[float] = []
    for scenario_name, mttr_list in results.items():
        if not mttr_list:
            print(f"  {scenario_name:<32}  no data")
            continue

        mean   = statistics.mean(mttr_list)
        median = statistics.median(mttr_list)
        p95    = sorted(mttr_list)[int(len(mttr_list) * 0.95)] if len(mttr_list) > 1 else mttr_list[0]
        lo     = min(mttr_list)
        hi     = max(mttr_list)
        n      = len(mttr_list)
        all_mttr.extend(mttr_list)

        print(f"  {scenario_name:<32} {_fmt(mean):>7} {_fmt(median):>7} {_fmt(p95):>7} {_fmt(lo):>7} {_fmt(hi):>7}  {n}/{RUNS_PER_SCENARIO}")

    print()
    if all_mttr:
        overall_mean   = statistics.mean(all_mttr)
        overall_median = statistics.median(all_mttr)
        overall_p95    = sorted(all_mttr)[int(len(all_mttr) * 0.95)]
        print(f"  {'OVERALL':<32} {_fmt(overall_mean):>7} {_fmt(overall_median):>7} {_fmt(overall_p95):>7}")

    print()
    print("  Industry comparison (MTTR from alert to root-cause identified)")
    print(f"  {'System':<38} {'MTTR':>8}")
    print(f"  {'-'*38} {'-'*8}")
    if all_mttr:
        print(f"  {'This system (mean)':<38} {_fmt(statistics.mean(all_mttr)):>8}")
    print(f"  {'Rootly AI SRE (published)':<38} {'< 90s':>8}")
    print(f"  {'Human SRE average (published)':<38} {'~1680s':>8}")
    print("=" * 62)


if __name__ == "__main__":
    try:
        asyncio.run(run_benchmark())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted.")
        sys.exit(1)
