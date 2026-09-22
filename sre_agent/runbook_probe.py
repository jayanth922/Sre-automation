#!/usr/bin/env python3
"""Run the runbook's own PromQL before the specialist's first model turn.

Quoting the runbook's query to the model got it executed — and still lost the
incident. In the 2026-09-22 ``inventory_slow_queries`` trial the metrics lane
ran the runbook's ``db_query_duration_seconds_bucket`` p90 *at the alert
timestamp*, because the brief said to query around the alert and never to
query "now". The harness stamps that alert at the instant the fault is
injected, so the five-minute rate window held only pre-fault traffic: 0.0221s
against a 1.0s threshold, which is the runbook's healthy branch. A live 2.1s
regression was escalated as "no action required"; the metric crossed the
threshold 35 seconds later.

Choosing the evaluation instant for a rate window is arithmetic, not
judgement, so it should not be a model's to get wrong. This module evaluates
the runbook's own expressions over ``[alert - lookback, now]`` and reports
first, peak and latest for each, before the lane spends a turn:

* the window covers the fault wherever the alert happens to be stamped;
* the expressions run exactly as the runbook wrote them, so no relabelling
  turns a matching series into an empty result;
* it costs no model call — extraction is text matching (``runbook_queries``)
  and the probe is a direct tool call.

A probe that fails, times out or returns nothing says so and never blocks the
lane: the model keeps every tool it had before.

Results come back from the tenant's own Prometheus, so the caller wraps them
as untrusted content before they reach a model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .prompt_guard import wrap_untrusted
from .runbook_queries import extract_promql

logger = logging.getLogger(__name__)

# Two queries answer "is the alerting signal actually over threshold?" for
# every runbook in the corpus; the rest of a runbook's PromQL belongs to
# branches the lane has not reached yet.
MAX_PROBED_QUERIES = 2
# Enough lead-in to show what the signal looked like before the fault.
PROBE_LOOKBACK = timedelta(minutes=5)
# namespace_scope caps an investigation read at 30 minutes; stay inside it.
MAX_PROBE_WINDOW = timedelta(minutes=28)
PROBE_STEP = "30s"
PROBE_TIMEOUT_SECONDS = 20.0
_MAX_SERIES = 3
_MAX_LABEL_CHARS = 120
_MAX_ERROR_CHARS = 200
_METRIC_RANGE_TOOL = "get_metric_range"
# str -> content blocks -> str -> {"result": [...]} is four unwraps deep.
_MAX_UNWRAP_DEPTH = 6


def probe_window(
    alert_started_at: Optional[datetime], *, now: datetime
) -> Tuple[datetime, datetime]:
    """``[alert - 5m, now]``, clamped to the runtime's investigation cap.

    Ending at *now* is the whole point: an alert stamped at fault injection
    has no fault inside its own rate window. Starting before the alert keeps
    the healthy baseline visible, so "it changed" is readable from one probe.
    """
    end = now
    start = end - MAX_PROBE_WINDOW
    if alert_started_at is not None:
        candidate = alert_started_at - PROBE_LOOKBACK
        if candidate > start:
            start = candidate
    # Clock skew, or an alert stamped in the future, must not invert the range.
    if start >= end:
        start = end - PROBE_LOOKBACK
    return start, end


def _is_content_block(item: Any) -> bool:
    """A LangChain/MCP content block, not a Prometheus series.

    Both arrive as a list of dicts, so tell them apart by what a series
    always carries and a content block never does.
    """
    return (
        isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and "values" not in item
        and "value" not in item
        and "metric" not in item
    )


def _content_block_text(payload: Any) -> Optional[str]:
    """The joined text of a content-block list, or None if it isn't one."""
    blocks = [payload] if isinstance(payload, dict) else payload
    if not isinstance(blocks, list) or not blocks:
        return None
    if not all(_is_content_block(item) for item in blocks):
        return None
    return "".join(item["text"] for item in blocks)


def _looks_like_series(item: Dict[str, Any]) -> bool:
    return "values" in item or "value" in item or "metric" in item


def _payload_series(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Best-effort read of the metrics MCP payload as a list of series.

    The bound tool declares ``response_format="content_and_artifact"``, so
    ``ainvoke`` hands back LangChain content blocks -- a *list of dicts*,
    exactly the shape a series list has. Unwrap before parsing, or every
    block is read as a series with no samples and a live fault reports
    healthy.
    """
    payload: Any = raw
    for _ in range(_MAX_UNWRAP_DEPTH):
        # response_format="content_and_artifact" can also surface as a
        # (content, artifact) pair.
        if isinstance(payload, tuple) and payload:
            payload = payload[0]
            continue
        if isinstance(payload, str):
            text = payload.strip()
            if not text or text.lower().startswith("error"):
                return None
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                return None
            continue
        block_text = _content_block_text(payload)
        if block_text is not None:
            payload = block_text
            continue
        # The MCP server wraps the list as {"result": [...]}, and
        # Prometheus' own envelope ({"data": {"result": [...]}}) survives a
        # passthrough.
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
            continue
        if isinstance(payload, dict) and isinstance(payload.get("result"), list):
            payload = payload["result"]
            continue
        break

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return None
    series = [item for item in payload if isinstance(item, dict)]
    if series and not any(_looks_like_series(item) for item in series):
        # Whatever this is, it is not a series list. Calling it "no samples"
        # would report a live fault as healthy; surface the raw text so the
        # lane sees an unreadable answer for what it is.
        return None
    return series


def _points(series: Dict[str, Any]) -> List[Tuple[float, float]]:
    raw_points: Sequence[Any]
    if isinstance(series.get("values"), list):
        raw_points = series["values"]
    elif isinstance(series.get("value"), (list, tuple)):
        raw_points = [series["value"]]
    else:
        return []
    points: List[Tuple[float, float]] = []
    for item in raw_points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            timestamp, value = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        # "NaN" and "+Inf" are real Prometheus answers and float() accepts
        # both; a single one of them would make the reported peak "nan".
        if not math.isfinite(value):
            continue
        points.append((timestamp, value))
    return points


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M:%SZ")


def _labels(series: Dict[str, Any]) -> str:
    metric = series.get("metric")
    if not isinstance(metric, dict) or not metric:
        return "{}"
    rendered = ",".join(
        f'{key}="{value}"' for key, value in sorted(metric.items()) if key != "__name__"
    )
    if len(rendered) > _MAX_LABEL_CHARS:
        rendered = rendered[:_MAX_LABEL_CHARS] + "…"
    return "{" + rendered + "}"


def summarize_probe(raw: Any) -> str:
    """First, peak and latest per series — the shape a threshold is read from.

    A hundred raw samples per series would cost more context than the lane's
    whole brief and still leave the model to find the maximum. These three
    numbers answer "was it ever over the line, and is it over it now?".
    """
    series_list = _payload_series(raw)
    if series_list is None:
        text = str(raw or "").strip()
        if not text:
            return "no result returned"
        return text[:_MAX_ERROR_CHARS]
    if not series_list:
        return "no series returned (the query matched nothing over this window)"
    lines: List[str] = []
    for series in series_list[:_MAX_SERIES]:
        points = _points(series)
        if not points:
            lines.append(f"{_labels(series)}: no samples")
            continue
        peak_at, peak = max(points, key=lambda point: point[1])
        first_at, first = points[0]
        last_at, last = points[-1]
        lines.append(
            f"{_labels(series)}: first {first:.4g} at {_clock(first_at)}, "
            f"peak {peak:.4g} at {_clock(peak_at)}, "
            f"latest {last:.4g} at {_clock(last_at)} ({len(points)} samples)"
        )
    dropped = len(series_list) - len(lines)
    if dropped > 0:
        lines.append(f"({dropped} further series not shown)")
    return "\n".join(lines)


def metrics_probe_caller(
    tools: Optional[Sequence[Any]],
) -> Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]]:
    """An async caller over the lane's *already bound* metrics tool.

    Reusing the bound tool keeps the probe inside the same tenant scoping,
    argument gate and audit trail as any model-issued call, and opens no
    second MCP connection.
    """
    by_name = {getattr(tool, "name", ""): tool for tool in tools or []}
    if _METRIC_RANGE_TOOL not in by_name:
        return None

    async def _call(tool_name: str, args: Dict[str, Any]) -> Any:
        tool = by_name.get(tool_name)
        if tool is None:
            raise RuntimeError(f"tool '{tool_name}' is not bound to this lane")
        invoke = getattr(tool, "ainvoke", None)
        if invoke is not None:
            return await invoke(args)
        return tool.invoke(args)

    return _call


async def probe_runbook_queries(
    runbook_text: str,
    *,
    tool_caller: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]],
    alert_started_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
    limit: int = MAX_PROBED_QUERIES,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> str:
    """Evaluate the runbook's own PromQL over the incident window.

    Returns a brief-ready block, or ``""`` when there is nothing to say —
    no runbook query, no metrics tool, or every probe failed.
    """
    if tool_caller is None:
        return ""
    queries = extract_promql(runbook_text)[: max(0, int(limit))]
    if not queries:
        return ""
    start, end = probe_window(alert_started_at, now=now or datetime.now(timezone.utc))
    results: List[str] = []
    measured = 0
    for query in queries:
        try:
            raw = await asyncio.wait_for(
                tool_caller(
                    _METRIC_RANGE_TOOL,
                    {
                        "query": query,
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "step": PROBE_STEP,
                    },
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("Runbook probe timed out after %.0fs: %s", timeout_seconds, query)
            results.append(f"{query}\n  probe timed out after {timeout_seconds:.0f}s")
            continue
        except Exception as exc:  # a probe is never allowed to fail the lane
            logger.warning("Runbook probe failed (%s): %s", type(exc).__name__, exc)
            detail = str(exc)[:_MAX_ERROR_CHARS]
            results.append(f"{query}\n  probe failed: {type(exc).__name__}: {detail}")
            continue
        measured += 1
        summary = summarize_probe(raw)
        indented = "\n".join(f"  {line}" for line in summary.splitlines())
        results.append(f"{query}\n{indented}")
    if not measured:
        # Every probe errored; the model still has the tools, and saying
        # "probe failed" is more honest than a silent omission.
        logger.info("Runbook probe produced no measurements for this lane")
    if not results:
        return ""
    payload = "\n".join(results)
    return "\n".join(
        [
            "Runbook queries already executed for you over "
            f"{start.isoformat(timespec='seconds')} → "
            f"{end.isoformat(timespec='seconds')} (UTC), verbatim, with the "
            "window covering the alert and everything since. Read the "
            "runbook's thresholds against THESE numbers. An instant query "
            "stamped at the alert time would evaluate a rate window that "
            "mostly precedes the fault and can read healthy while the "
            "incident is live, so do not re-run these at the alert "
            "timestamp; re-run one only to check a change you have made.",
            wrap_untrusted(
                "runbook_query_probe", payload, max_len=len(payload) + 1
            ),
        ]
    )
