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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class VerificationOutcome:
    status: str                       # RESOLVED | FAILED | UNKNOWN
    original_value: Optional[float]
    current_value: Optional[float]
    threshold: Optional[float]
    improvement_pct: float = 0.0
    detail: str = ""


def parse_prom_value(resp: Any) -> Optional[float]:
    """Extract a scalar from a Prometheus MCP response (JSON str / TextContent / list)."""
    data = resp
    try:
        if isinstance(resp, str):
            data = json.loads(resp)
        elif hasattr(resp, "text"):
            data = json.loads(resp.text)
        elif isinstance(resp, list) and resp and hasattr(resp[0], "text"):
            data = json.loads(resp[0].text)
    except (ValueError, TypeError):
        return None

    if isinstance(data, list) and data:
        first = data[0]
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
    if isinstance(data, (int, float)):
        return float(data)
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
