#!/usr/bin/env python3
"""
Agent observability (interview Q5: how do you observe agents?).

Web-era observability (Datadog/Prometheus/Grafana) watches services; agents need
a different lens — per-node timings, where the reasoning failed, which model
provider served each step, and when the runtime fell back to another provider.
This module records structured agent events and rolls them into a summary you can
surface on a dashboard or ship to an LLM-observability backend.

Pure/stdlib and testable; the `track` context manager wraps a node to capture its
duration and any exception (as a failure trace) without changing the node's logic.
"""

from __future__ import annotations

import logging
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    node: str
    event: str                     # start | end | error | provider_switch | tool_call
    incident_id: Optional[str] = None
    duration_ms: Optional[float] = None
    detail: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ObservabilityRecorder:
    """In-process ring of agent events + a rollup summary."""

    def __init__(self, max_events: int = 2000) -> None:
        self._events: List[AgentEvent] = []
        self._max = max_events

    def record(self, event: AgentEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max :]

    def record_provider_switch(self, node: str, from_provider: str, to_provider: str, incident_id: Optional[str] = None) -> None:
        self.record(AgentEvent(node, "provider_switch", incident_id, detail=f"{from_provider}→{to_provider}"))

    def events(self) -> List[AgentEvent]:
        return list(self._events)

    def summary(self) -> Dict[str, Any]:
        nodes: Dict[str, Dict[str, Any]] = {}
        failures: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []

        for ev in self._events:
            n = nodes.setdefault(ev.node, {"runs": 0, "errors": 0, "total_ms": 0.0})
            if ev.event == "end":
                n["runs"] += 1
                n["total_ms"] += ev.duration_ms or 0.0
            elif ev.event == "error":
                n["errors"] += 1
                failures.append({"node": ev.node, "incident_id": ev.incident_id, "detail": ev.detail, "at": ev.timestamp})
            elif ev.event == "provider_switch":
                switches.append({"node": ev.node, "detail": ev.detail, "at": ev.timestamp})

        for n in nodes.values():
            n["avg_ms"] = round(n["total_ms"] / n["runs"], 1) if n["runs"] else 0.0

        total_runs = sum(n["runs"] for n in nodes.values())
        total_errors = sum(n["errors"] for n in nodes.values())
        return {
            "nodes": nodes,
            "failures": failures,
            "provider_switches": switches,
            "total_runs": total_runs,
            "total_errors": total_errors,
            "error_rate": round(total_errors / total_runs, 3) if total_runs else 0.0,
        }


_GLOBAL_RECORDER: Optional[ObservabilityRecorder] = None


def get_recorder() -> ObservabilityRecorder:
    global _GLOBAL_RECORDER
    if _GLOBAL_RECORDER is None:
        _GLOBAL_RECORDER = ObservabilityRecorder()
    return _GLOBAL_RECORDER


@contextmanager
def track(recorder: ObservabilityRecorder, node: str, incident_id: Optional[str] = None):
    """Time a node and capture any exception as a failure trace, then re-raise.

    Works around an ``await`` too — the block wraps the awaited call.
    """
    recorder.record(AgentEvent(node, "start", incident_id))
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        recorder.record(AgentEvent(
            node, "error", incident_id,
            duration_ms=(time.perf_counter() - start) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
        ))
        raise
    else:
        recorder.record(AgentEvent(
            node, "end", incident_id,
            duration_ms=(time.perf_counter() - start) * 1000.0,
        ))
