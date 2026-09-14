#!/usr/bin/env python3
"""
Remediation verification — did the fix actually work?

After the ACT phase applies a remediation, the loop is only closed if we confirm
the incident's signal returned to normal. This module re-queries the metric and
decides RESOLVED vs FAILED, plus the improvement percentage. It replaces the
orphaned (unreachable) verification code that used to sit in the Planner node.

Pure decision logic (`evaluate_verification`) is unit-tested; `verify_remediation`
adds the metric fetch through an injected tool_caller (the Prometheus MCP), so it
is testable without a live cluster.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class VerificationOutcome:
    status: str                       # RESOLVED | FAILED | UNKNOWN
    original_value: Optional[float]
    current_value: Optional[float]
    threshold: Optional[float]
    improvement_pct: float = 0.0
    detail: str = ""


def content_block_text(item: Any) -> Optional[str]:
    """The payload string of one MCP content block, or None if it isn't one.

    Two shapes arrive here. The MCP SDK's ``TextContent`` exposes ``.text``;
    ``langchain_mcp_adapters`` — what ``executor.build_mcp_tool_caller``
    actually returns from ``tool.ainvoke`` — hands back the same block already
    flattened to a plain dict, ``{"type": "text", "text": "…", "id": "lc_…"}``.

    Only the object form used to be recognised, so in production the dict form
    fell through to "this is already the series list" and verification parsed
    the *envelope* instead of the payload. A one-element list is never empty,
    and ``verify_alert_cleared`` reads non-empty as "the alert is still
    firing" — so every live remediation was graded FAILED, including the ones
    that demonstrably worked, and the alert-state poll could not have returned
    RESOLVED for any input. On the scalar path the same envelope produced "no
    current metric value" (UNKNOWN).

    A Prometheus series is a dict too, so the test is specifically for a
    string ``text`` field: series carry ``metric``/``value``/``values``.
    """
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(item, dict):
        text = item.get("text")
        if isinstance(text, str):
            return text
    return None


def parse_prom_result(resp: Any) -> Optional[List[Any]]:
    """Return the instant-query series list, or None if this isn't one.

    Three shapes have to stay distinguishable, because verification means
    something different for each:

    - ``{"result": [...], "series_total": N}`` — what the Prometheus MCP
      actually returns; it wraps the vector to cap series count and context
      size (``prometheus_real/server.py::_cap_vector_result``). Reading only
      the bare-list shape is why every live verification came back "no current
      metric value" regardless of what the metric said.
    - ``[]`` — a real answer: nothing matches right now. For an ``ALERTS``
      query that *is* the resolution signal, so it must not collapse to None.
    - ``"Error querying metric: …"`` — the server or the query failed, and we
      know nothing. None.

    Any of the three may arrive wrapped in an MCP content block (see
    ``content_block_text``), which is unwrapped first.
    """
    data = resp
    block = content_block_text(data)
    if block is None and isinstance(data, list) and data:
        block = content_block_text(data[0])
    if block is not None:
        data = block

    if isinstance(data, str):
        stripped = data.strip()
        if stripped[:5].lower() == "error":
            return None
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            return None

    if isinstance(data, dict):
        inner = data.get("result")
        return inner if isinstance(inner, list) else None
    if isinstance(data, list):
        return data
    return None


def parse_prom_value(resp: Any) -> Optional[float]:
    """Extract a scalar from a Prometheus MCP response (JSON str / TextContent / list)."""
    series = parse_prom_result(resp)
    if series:
        first = series[0]
        if isinstance(first, dict):
            if "value" in first and isinstance(first["value"], list) and len(first["value"]) >= 2:
                try:
                    return float(first["value"][1])
                except (ValueError, TypeError):
                    return None
            if "values" in first and first["values"]:
                last = first["values"][-1]
                if isinstance(last, list) and len(last) >= 2:
                    try:
                        return float(last[1])
                    except (ValueError, TypeError):
                        return None
    if isinstance(resp, (int, float)) and not isinstance(resp, bool):
        return float(resp)
    return None


def evaluate_verification(
    original: Optional[float], current: Optional[float], threshold: Optional[float]
) -> VerificationOutcome:
    """Decide RESOLVED/FAILED from the current value vs the alert threshold."""
    if current is None:
        return VerificationOutcome("UNKNOWN", original, current, threshold, 0.0, "no current metric value")

    improvement = 0.0
    if original is not None and original > 0:
        improvement = ((original - current) / original) * 100.0

    if threshold is None:
        status = "UNKNOWN"
        detail = "no threshold to compare against"
    elif current < threshold:
        status = "RESOLVED"
        detail = f"current {current:.4g} < threshold {threshold:.4g}"
    else:
        status = "FAILED"
        detail = f"current {current:.4g} >= threshold {threshold:.4g}"

    return VerificationOutcome(status, original, current, threshold, improvement, detail)


async def verify_remediation(
    promql: str,
    threshold: Optional[float],
    tool_caller: Callable[[str, Dict[str, Any]], Any],
    original_value: Optional[float] = None,
    wait_seconds: int = 0,
    metric_tool: str = "get_metric",
) -> VerificationOutcome:
    """Wait for propagation, re-query the metric, and evaluate the outcome."""
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    try:
        resp = await tool_caller(metric_tool, {"query": promql})
    except Exception as e:
        return VerificationOutcome("UNKNOWN", original_value, None, threshold, 0.0, f"metric query failed: {e}")
    current = parse_prom_value(resp)
    outcome = evaluate_verification(original_value, current, threshold)
    logger.info(f"✅ Verification: {outcome.status} ({outcome.detail})")
    return outcome


def alert_state_promql(alert_name: str, service: str = "") -> str:
    """The query that answers "is the thing that opened this incident over?"."""
    selector = f'alertname="{alert_name}",alertstate="firing"'
    if service and service != "unknown":
        selector += f',service="{service}"'
    return f"ALERTS{{{selector}}}"


async def verify_alert_cleared(
    alert_name: str,
    service: str,
    tool_caller: Callable[[str, Dict[str, Any]], Any],
    *,
    settle_seconds: int = 0,
    timeout_seconds: int = 300,
    poll_seconds: int = 30,
    metric_tool: str = "get_metric",
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> VerificationOutcome:
    """Wait for the incident's own alert to stop firing, or time out saying so.

    The alert rule is the only threshold in the system that isn't a guess: it
    is what declared the incident, and it carries the service owner's real
    objective (p90 DB latency > 1s, memory > 200MB, 5xx ratio > 10%). Grading
    every remediation against one hardcoded error-rate expression instead
    checked a metric the service may not even emit, and could call a latency
    fix "resolved" because errors happened to be low.

    Polling, not a single sample, because a remediation is not instant: a
    ``kubectl set env`` rolls a new pod, and the alert's own ``rate(...[5m])``
    window still contains the fault for minutes afterwards. Asking once and
    immediately is how a fix that worked gets recorded as UNKNOWN.
    """
    promql = alert_state_promql(alert_name, service)
    deadline_budget = max(0, int(timeout_seconds))
    interval = max(1, int(poll_seconds))
    waited = max(0, int(settle_seconds))
    if waited:
        await sleep(waited)

    last_detail = "never queried"
    while True:
        try:
            resp = await tool_caller(metric_tool, {"query": promql})
        except Exception as exc:  # unreachable Prometheus is not a verdict
            return VerificationOutcome(
                "UNKNOWN", 1.0, None, 1.0, 0.0, f"alert state query failed: {exc}"
            )

        series = parse_prom_result(resp)
        if series is None:
            last_detail = "alert state unreadable from Prometheus"
        elif not series:
            detail = f"alert {alert_name} is no longer firing after {waited}s"
            logger.info(f"✅ Verification: RESOLVED ({detail})")
            return VerificationOutcome("RESOLVED", 1.0, 0.0, 1.0, 100.0, detail)
        else:
            last_detail = f"alert {alert_name} still firing after {waited}s"

        if waited >= deadline_budget:
            break
        step = min(interval, deadline_budget - waited)
        await sleep(step)
        waited += step

    if series is None:
        logger.info(f"✅ Verification: UNKNOWN ({last_detail})")
        return VerificationOutcome("UNKNOWN", 1.0, None, 1.0, 0.0, last_detail)
    logger.info(f"✅ Verification: FAILED ({last_detail})")
    return VerificationOutcome("FAILED", 1.0, 1.0, 1.0, 0.0, last_detail)
