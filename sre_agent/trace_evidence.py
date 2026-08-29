#!/usr/bin/env python3
"""A08 root-run and child-span evidence with privacy-safe defaults."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # pragma: no cover - dependency-light environments

    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
REQUIRED_SPAN_KINDS = frozenset(
    {
        "model",
        "retrieval",
        "tool",
        "policy",
        "approval",
        "mutation",
        "verification",
    }
)
REQUIRED_ATTRIBUTES = {
    "root": frozenset(
        {
            "gen_ai.operation.name",
            "gen_ai.workflow.name",
            "gen_ai.conversation.id",
            "sentinel.run_manifest.id",
            "sentinel.job.id",
        }
    ),
    "model": frozenset(
        {
            "gen_ai.operation.name",
            "gen_ai.provider.name",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "sentinel.usage.total_tokens",
            "sentinel.cost.usd",
        }
    ),
    "retrieval": frozenset({"gen_ai.operation.name", "sentinel.retrieval.outcome"}),
    "tool": frozenset(
        {"gen_ai.operation.name", "gen_ai.tool.name", "gen_ai.tool.type"}
    ),
    "policy": frozenset({"gen_ai.operation.name", "sentinel.policy.decision"}),
    "approval": frozenset({"gen_ai.operation.name", "sentinel.approval.outcome"}),
    "mutation": frozenset(
        {
            "gen_ai.operation.name",
            "sentinel.mutation.live_enabled",
            "sentinel.mutation.count",
        }
    ),
    "verification": frozenset(
        {"gen_ai.operation.name", "sentinel.verification.outcome"}
    ),
}
_TRACE_KEYS = (
    "root_trace_id",
    "run_manifest_id",
    "incident_id",
    "job_id",
)


@dataclass
class _PendingTool:
    span_id: str
    parent_span_id: Optional[str]
    name: str
    root_trace_id: str
    trace: dict[str, Optional[str]]
    started_at: str
    started_monotonic: float
    input_payload: Optional[str]


def _artifact_path() -> Path:
    configured = os.getenv("TRACE_EVIDENCE_PATH", "").strip()
    return Path(configured or "reports/run-trace.jsonl")


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _payload_capture_enabled() -> bool:
    return os.getenv("TRACE_PAYLOAD_CAPTURE", "").lower() in {"1", "true", "yes"}


def _capture_payload(value: Any) -> Optional[str]:
    if value is None or not _payload_capture_enabled():
        return None
    from .prompt_guard import sanitize_untrusted

    maximum = int(os.getenv("TRACE_PAYLOAD_MAX_CHARS", "2000"))
    maximum = min(max(maximum, 64), 10000)
    return sanitize_untrusted(value, max_len=maximum)


def _safe_attributes(attributes: Optional[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if not isinstance(key, str) or not key.strip() or value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


class RunTraceRecorder:
    """Append-only metadata spans indexed in memory for per-run summaries."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._roots: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._records = []
            self._roots = {}

    def _append(self, record: dict[str, Any]) -> None:
        encoded = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            path = _artifact_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)
        except OSError as exc:
            record.setdefault("completeness_reasons", []).append(
                f"artifact_write_failed:{type(exc).__name__}"
            )
            logger.error("Run trace artifact write failed: %s", exc)
        with self._lock:
            self._records.append(record)

    def start_run(
        self,
        *,
        root_trace_id: str,
        run_manifest_id: str,
        incident_id: str,
        job_id: str,
    ) -> None:
        trace_id = _text(root_trace_id)
        if not all(
            _text(value) for value in (trace_id, run_manifest_id, incident_id, job_id)
        ):
            raise ValueError("root run requires trace, manifest, incident, and job IDs")
        root = {
            "run_manifest_id": str(run_manifest_id),
            "incident_id": str(incident_id),
            "job_id": str(job_id),
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error_type": None,
        }
        with self._lock:
            self._roots[trace_id] = root
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "root_start",
                "root_trace_id": trace_id,
                "span_id": trace_id,
                "parent_span_id": None,
                "name": "sentinel incident run",
                "span_kind": "root",
                "status": "running",
                "started_at": root["started_at"],
                "finished_at": None,
                "duration_ms": None,
                "attributes": {
                    "gen_ai.operation.name": "invoke_workflow",
                    "gen_ai.workflow.name": "sentinel_incident_investigation",
                    "gen_ai.conversation.id": str(incident_id),
                    "sentinel.run_manifest.id": str(run_manifest_id),
                    "sentinel.job.id": str(job_id),
                },
                "payload": None,
                "completeness_reasons": [],
            }
        )

    def record_span(
        self,
        *,
        root_trace_id: str,
        span_kind: str,
        name: str,
        status: str = "success",
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        started_at: Optional[str] = None,
        duration_ms: Optional[float] = None,
        attributes: Optional[dict[str, Any]] = None,
        payload: Any = None,
    ) -> None:
        if span_kind not in REQUIRED_SPAN_KINDS:
            raise ValueError(f"unsupported trace span kind: {span_kind}")
        if status not in {"success", "error", "blocked", "not_applicable"}:
            raise ValueError(f"unsupported trace span status: {status}")
        trace_id = _text(root_trace_id)
        if not trace_id:
            raise ValueError("root_trace_id is required for child spans")
        started = started_at or datetime.now(timezone.utc).isoformat()
        semantic = {
            "gen_ai.operation.name": {
                "model": "chat",
                "retrieval": "retrieval",
                "tool": "execute_tool",
            }.get(span_kind, "invoke_workflow"),
            "gen_ai.workflow.name": "sentinel_incident_investigation",
            "sentinel.span.kind": span_kind,
        }
        semantic.update(_safe_attributes(attributes))
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "child_span",
                "root_trace_id": trace_id,
                "span_id": span_id or uuid.uuid4().hex,
                "parent_span_id": parent_span_id or trace_id,
                "name": _text(name) or span_kind,
                "span_kind": span_kind,
                "status": status,
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": (
                    round(float(duration_ms), 3) if duration_ms is not None else 0.0
                ),
                "attributes": semantic,
                "payload": _capture_payload(payload),
                "completeness_reasons": [],
            }
        )

    def finish_run(
        self,
        root_trace_id: str,
        *,
        status: str,
        error_type: Optional[str] = None,
    ) -> None:
        if status not in {"success", "error"}:
            raise ValueError("root run finalization status is invalid")
        trace_id = _text(root_trace_id)
        if not trace_id:
            raise ValueError("root_trace_id is required")
        finished = datetime.now(timezone.utc).isoformat()
        with self._lock:
            root = self._roots.setdefault(trace_id, {})
            root.update(
                {
                    "status": status,
                    "finished_at": finished,
                    "error_type": _text(error_type),
                }
            )
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "root_end",
                "root_trace_id": trace_id,
                "span_id": trace_id,
                "parent_span_id": None,
                "name": "sentinel incident run",
                "span_kind": "root",
                "status": status,
                "started_at": None,
                "finished_at": finished,
                "duration_ms": None,
                "attributes": {
                    "gen_ai.operation.name": "invoke_workflow",
                    "gen_ai.workflow.name": "sentinel_incident_investigation",
                },
                "payload": None,
                "error_type": _text(error_type),
                "completeness_reasons": [],
            }
        )

    def records(
        self, root_trace_id: Optional[str] = None
    ) -> tuple[dict[str, Any], ...]:
        with self._lock:
            values = tuple(dict(record) for record in self._records)
        if root_trace_id is None:
            return values
        return tuple(
            record for record in values if record["root_trace_id"] == root_trace_id
        )

    def root_trace_ids(self, *, incident_id: Optional[str] = None) -> tuple[str, ...]:
        """Return roots in start order, optionally scoped to one incident."""
        with self._lock:
            roots = tuple(
                (trace_id, dict(root)) for trace_id, root in self._roots.items()
            )
        return tuple(
            trace_id
            for trace_id, root in roots
            if incident_id is None or root.get("incident_id") == incident_id
        )

    def summary(
        self,
        *,
        root_trace_id: str,
        model_accounting: dict[str, Any],
    ) -> dict[str, Any]:
        records = list(self.records(root_trace_id))
        with self._lock:
            root = dict(self._roots.get(root_trace_id, {}))
        spans = [record for record in records if record["record_type"] == "child_span"]
        observed = {record["span_kind"] for record in spans}
        missing = sorted(REQUIRED_SPAN_KINDS - observed)
        reasons = [f"missing_span:{kind}" for kind in missing]
        if not root:
            reasons.append("root_span_missing")
        elif root.get("status") == "running":
            reasons.append("root_span_not_finalized")
        elif root.get("status") != "success":
            reasons.append("root_span_failed")
        reasons.extend(
            f"{record['record_type']}:{reason}"
            for record in records
            for reason in record.get("completeness_reasons", [])
        )
        root_starts = [
            record for record in records if record["record_type"] == "root_start"
        ]
        if root_starts:
            missing_root_attributes = REQUIRED_ATTRIBUTES["root"] - set(
                root_starts[-1]["attributes"]
            )
            reasons.extend(
                f"missing_attribute:root:{key}"
                for key in sorted(missing_root_attributes)
            )
        for record in spans:
            missing_attributes = REQUIRED_ATTRIBUTES[record["span_kind"]] - set(
                record["attributes"]
            )
            reasons.extend(
                f"missing_attribute:{record['span_kind']}:{key}"
                for key in sorted(missing_attributes)
            )
        if model_accounting.get("complete") is not True:
            reasons.extend(
                f"model_accounting:{reason}"
                for reason in model_accounting.get("completeness_reasons", [])
            )
        reasons = sorted(set(reasons))
        canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
        complete = not reasons
        return {
            "schema_version": SCHEMA_VERSION,
            "root_trace_id": root_trace_id,
            "complete": complete,
            "completeness_reasons": reasons,
            "required_span_kinds": sorted(REQUIRED_SPAN_KINDS),
            "observed_span_kinds": sorted(observed),
            "spans": len(spans),
            "span_counts": {
                kind: sum(record["span_kind"] == kind for record in spans)
                for kind in sorted(observed)
            },
            "error_spans": sum(record["status"] == "error" for record in spans),
            "cost_usd": model_accounting.get("cost_usd") if complete else None,
            "tokens": model_accounting.get("tokens") if complete else None,
            "model_latency_ms": (
                model_accounting.get("latency_ms") if complete else None
            ),
            "payload_capture": "redacted" if _payload_capture_enabled() else "off",
            "artifact_path": str(_artifact_path()),
            "records_sha256": (
                hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if records
                else None
            ),
        }


_RECORDER = RunTraceRecorder()


def get_run_trace_recorder() -> RunTraceRecorder:
    return _RECORDER


def record_span_from_state(
    state: Any,
    *,
    span_kind: str,
    name: str,
    status: str = "success",
    attributes: Optional[dict[str, Any]] = None,
    payload: Any = None,
) -> None:
    metadata = state.get("metadata", {}) if isinstance(state, dict) else {}
    root_trace_id = _text((metadata or {}).get("root_trace_id"))
    if not root_trace_id:
        return
    get_run_trace_recorder().record_span(
        root_trace_id=root_trace_id,
        span_kind=span_kind,
        name=name,
        status=status,
        attributes=attributes,
        payload=payload,
    )


class RunTraceCallback(BaseCallbackHandler):
    """Graph callback for external tool spans; payload capture is opt-in."""

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[str, _PendingTool] = {}
        self._lock = threading.Lock()

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        values = metadata or {}
        root_trace_id = _text(values.get("root_trace_id"))
        if not root_trace_id:
            return
        name = _text(serialized.get("name")) or "tool"
        pending = _PendingTool(
            span_id=str(run_id),
            parent_span_id=_text(parent_run_id),
            name=name,
            root_trace_id=root_trace_id,
            trace={key: _text(values.get(key)) for key in _TRACE_KEYS},
            started_at=datetime.now(timezone.utc).isoformat(),
            started_monotonic=time.perf_counter(),
            input_payload=_capture_payload(input_str),
        )
        with self._lock:
            self._pending[str(run_id)] = pending

    def _finish(
        self,
        run_id: Any,
        *,
        status: str,
        output: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        with self._lock:
            pending = self._pending.pop(str(run_id), None)
        if pending is None:
            return
        payload = None
        if _payload_capture_enabled():
            payload = {
                "input": pending.input_payload,
                "output": _capture_payload(output),
            }
        attributes = {
            "gen_ai.tool.name": pending.name,
            "gen_ai.tool.type": "extension",
            "sentinel.error.type": type(error).__name__ if error else None,
            **{
                f"sentinel.{key.replace('_', '.')}": value
                for key, value in pending.trace.items()
                if value
            },
        }
        get_run_trace_recorder().record_span(
            root_trace_id=pending.root_trace_id,
            span_kind="tool",
            name=pending.name,
            status=status,
            span_id=pending.span_id,
            parent_span_id=pending.parent_span_id,
            started_at=pending.started_at,
            duration_ms=(time.perf_counter() - pending.started_monotonic) * 1000.0,
            attributes=attributes,
            payload=payload,
        )

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, status="success", output=output)

    def on_tool_error(
        self, error: BaseException, *, run_id: Any, **kwargs: Any
    ) -> None:
        del kwargs
        self._finish(run_id, status="error", error=error)


def trace_callbacks(base: dict[str, Any]) -> dict[str, Any]:
    """Attach the local tool-span callback when a root trace is configured."""
    config = dict(base)
    callbacks = list(config.get("callbacks", []))
    if not any(isinstance(item, RunTraceCallback) for item in callbacks):
        callbacks.append(RunTraceCallback())
    config["callbacks"] = callbacks
    return config
