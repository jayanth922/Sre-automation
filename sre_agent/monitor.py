#!/usr/bin/env python3
"""
Always-on health monitor (design slice #3).

The OODA "Observe" phase, run continuously instead of only on an alert. A loop
periodically pulls health signals (golden signals / SLO burn) via the metrics
MCP, evaluates them, publishes a live insight to the bus (so the dashboard shows
continuous health), and — on a *transition* into a degraded/critical state —
flags it: opens an incident and notifies on-call. Dedup by state transition so
it doesn't re-flag every tick.

Pure cores (`evaluate_health`, `MonitorState`) are unit-tested; the metric fetch
and the flag action are injected, so the loop is testable without infra.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class HealthReport:
    service: str
    status: str                       # OK | DEGRADED | CRITICAL
    flags: List[Dict[str, Any]] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_health(signals: Dict[str, Any]) -> HealthReport:
    """Classify current health from raw signals. Pure; thresholds env-tunable."""
    service = str(signals.get("service", "unknown"))
    err = float(signals.get("error_rate", 0.0) or 0.0)
    lat = float(signals.get("latency_p95", 0.0) or 0.0)
    sat = float(signals.get("saturation", 0.0) or 0.0)
    burn = float(signals.get("slo_burn_rate", 0.0) or 0.0)

    crit_err, warn_err = _f("MON_CRIT_ERROR", 0.30), _f("MON_WARN_ERROR", 0.05)
    lat_thresh = _f("MON_LATENCY_P95", 1.0)
    warn_sat, crit_sat = _f("MON_WARN_SATURATION", 0.80), _f("MON_CRIT_SATURATION", 0.95)
    fast_burn, warn_burn = _f("MON_FAST_BURN", 14.4), _f("MON_WARN_BURN", 6.0)

    flags: List[Dict[str, Any]] = []
    status = "OK"

    def flag(signal, value, threshold, level):
        nonlocal status
        flags.append({"signal": signal, "value": value, "threshold": threshold, "level": level})
        if level == "CRITICAL":
            status = "CRITICAL"
        elif status != "CRITICAL":
            status = "DEGRADED"

    if err >= crit_err:
        flag("error_rate", err, crit_err, "CRITICAL")
    elif err >= warn_err:
        flag("error_rate", err, warn_err, "DEGRADED")
    if burn >= fast_burn:
        flag("slo_burn_rate", burn, fast_burn, "CRITICAL")
    elif burn >= warn_burn:
        flag("slo_burn_rate", burn, warn_burn, "DEGRADED")
    if sat >= crit_sat:
        flag("saturation", sat, crit_sat, "CRITICAL")
    elif sat >= warn_sat:
        flag("saturation", sat, warn_sat, "DEGRADED")
    if lat >= lat_thresh:
        flag("latency_p95", lat, lat_thresh, "DEGRADED")

    summary = "healthy" if status == "OK" else (
        f"{service}: {status} — " + ", ".join(f"{fl['signal']}={fl['value']}" for fl in flags)
    )
    return HealthReport(service=service, status=status, flags=flags, signals=signals, summary=summary)


class MonitorState:
    """Tracks last status per target for transition-based (deduped) flagging."""

    def __init__(self) -> None:
        self._last: Dict[str, str] = {}

    def transitioned_to_flagged(self, target: str, status: str) -> bool:
        """True only when entering a flagged state from a different (e.g. OK) one."""
        previous = self._last.get(target, "OK")
        self._last[target] = status
        return status in ("DEGRADED", "CRITICAL") and status != previous


async def run_monitor(
    fetch: Callable[[], Awaitable[Dict[str, Any]]],
    on_flag: Optional[Callable[[HealthReport], Awaitable[Any]]] = None,
    bus=None,
    interval: float = 30.0,
    max_ticks: Optional[int] = None,
    state: Optional[MonitorState] = None,
) -> int:
    """Continuously observe, publish live insights, and flag on transition.

    ``fetch`` returns current signals; ``on_flag`` opens the incident / notifies
    on-call (both injected). ``max_ticks`` bounds the loop for tests.
    """
    from .live_events import publish_insight

    state = state or MonitorState()
    ticks = 0
    while True:
        signals = await fetch()
        report = evaluate_health(signals)
        await publish_insight(report.to_dict(), bus=bus)
        if state.transitioned_to_flagged(report.service, report.status) and on_flag:
            logger.info(f"🚨 Monitor: flagging {report.service} → {report.status}")
            await on_flag(report)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        if interval > 0:
            await asyncio.sleep(interval)
    return ticks


def build_alert_payload(report: HealthReport) -> Dict[str, Any]:
    """Shape a flagged HealthReport into an Alertmanager webhook payload, so the
    monitor's flag flows through the SAME incident-creation path as real alerts.
    Pure/testable."""
    from datetime import datetime, timezone

    severity = "critical" if report.status == "CRITICAL" else "warning"
    top = report.flags[0]["signal"] if report.flags else "health"
    return {
        "version": "4",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": f"Monitor{report.service.title().replace('-', '')}{top.title().replace('_', '')}",
                "severity": severity,
                "service": report.service,
            },
            "annotations": {"summary": report.summary,
                            "description": f"Proactively flagged by the always-on monitor: {report.summary}"},
            "startsAt": datetime.now(timezone.utc).isoformat(),
        }],
    }


async def default_on_flag(report: HealthReport, base_url: Optional[str] = None,
                          token: Optional[str] = None) -> Any:  # pragma: no cover - requires API
    """Open an incident for a flag (via the alerts webhook) and note on-call."""
    import httpx

    from .oncall import format_slack_mention, resolve_oncall

    base_url = base_url or os.getenv("AGENT_API_URL", "http://localhost:8080")
    token = token or os.getenv("CLUSTER_TOKEN", "")
    oncall = format_slack_mention(resolve_oncall())
    logger.info(f"🚨 Monitor flag → opening incident for {report.service}; paging {oncall}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{base_url}/api/v1/alerts/webhook",
                              json=build_alert_payload(report),
                              headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return {"opened": r.json(), "oncall": oncall}


async def run_monitor_service() -> None:  # pragma: no cover - requires infra
    """Entrypoint: continuously observe the metrics MCP and flag proactively."""
    from .executor import build_metrics_tool_caller

    caller = await build_metrics_tool_caller()

    async def fetch() -> Dict[str, Any]:
        # Best-effort golden-signals pull; shape into monitor signals.
        service = os.getenv("MONITOR_SERVICE", "checkout-service")
        resp = await caller("get_golden_signals", {"service": service, "namespace": os.getenv("MONITOR_NAMESPACE", "demo-app")})
        gs = resp if isinstance(resp, dict) else {}
        return {"service": service,
                "error_rate": float(gs.get("errors", 0) or 0),
                "latency_p95": float(gs.get("latency", 0) or 0),
                "saturation": float(gs.get("saturation", 0) or 0)}

    interval = float(os.getenv("MONITOR_INTERVAL_SECONDS", "30"))
    await run_monitor(fetch, on_flag=default_on_flag, interval=interval)
