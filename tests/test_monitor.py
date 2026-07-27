#!/usr/bin/env python3
"""Unit tests for the always-on monitor (design slice #3)."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.monitor import (  # noqa: E402
    HealthReport,
    MonitorState,
    build_alert_payload,
    evaluate_health,
    run_monitor,
)


def test_evaluate_ok():
    r = evaluate_health({"service": "checkout", "error_rate": 0.01, "saturation": 0.2})
    assert r.status == "OK" and r.flags == []


def test_evaluate_degraded_on_warn_error():
    r = evaluate_health({"service": "checkout", "error_rate": 0.10})
    assert r.status == "DEGRADED"
    assert any(f["signal"] == "error_rate" for f in r.flags)


def test_evaluate_critical_on_high_error_or_fast_burn():
    assert evaluate_health({"service": "s", "error_rate": 0.5}).status == "CRITICAL"
    assert evaluate_health({"service": "s", "slo_burn_rate": 20}).status == "CRITICAL"


def test_evaluate_latency_and_saturation_flags():
    r = evaluate_health({"service": "s", "latency_p95": 2.0, "saturation": 0.85})
    assert r.status == "DEGRADED"
    signals = {f["signal"] for f in r.flags}
    assert "latency_p95" in signals and "saturation" in signals


def test_monitor_state_flags_only_on_transition():
    st = MonitorState()
    assert st.transitioned_to_flagged("s", "OK") is False
    assert st.transitioned_to_flagged("s", "DEGRADED") is True   # OK → DEGRADED
    assert st.transitioned_to_flagged("s", "DEGRADED") is False  # no change
    assert st.transitioned_to_flagged("s", "CRITICAL") is True   # DEGRADED → CRITICAL
    assert st.transitioned_to_flagged("s", "OK") is False        # recovered, no flag


def test_run_monitor_publishes_and_flags_on_transitions():
    async def scenario():
        # Sequence of signals over 4 ticks: OK, DEGRADED, DEGRADED, CRITICAL.
        seq = [
            {"service": "checkout", "error_rate": 0.0},
            {"service": "checkout", "error_rate": 0.10},
            {"service": "checkout", "error_rate": 0.10},
            {"service": "checkout", "error_rate": 0.50},
        ]
        i = {"n": 0}

        async def fetch():
            s = seq[i["n"]]
            i["n"] += 1
            return s

        flagged = []

        async def on_flag(report: HealthReport):
            flagged.append(report.status)

        from sre_agent.live_events import InMemoryEventBus, INSIGHTS_CHANNEL
        bus = InMemoryEventBus()
        insights = bus.subscribe(INSIGHTS_CHANNEL)

        ticks = await run_monitor(fetch, on_flag=on_flag, bus=bus, interval=0, max_ticks=4)

        received = []
        for _ in range(4):
            received.append(await asyncio.wait_for(insights.get(), timeout=1))
        return ticks, flagged, received

    ticks, flagged, received = asyncio.run(scenario())
    assert ticks == 4
    # Flagged on OK→DEGRADED (tick2) and DEGRADED→CRITICAL (tick4); not tick3.
    assert flagged == ["DEGRADED", "CRITICAL"]
    # A live insight published every tick.
    assert len(received) == 4 and all(ev["type"] == "insight" for ev in received)


def test_build_alert_payload_from_flag():
    report = evaluate_health({"service": "checkout-service", "error_rate": 0.5})
    payload = build_alert_payload(report)
    alert = payload["alerts"][0]
    assert payload["status"] == "firing"
    assert alert["labels"]["severity"] == "critical"
    assert alert["labels"]["service"] == "checkout-service"
    assert "monitor" in alert["annotations"]["description"].lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
