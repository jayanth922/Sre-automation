#!/usr/bin/env python3
"""Trace-linked, fail-closed accounting for every routed model call.

Only metadata is recorded. Prompts and model outputs never enter the accounting
artifact. Provider-reported usage and cost are preserved as evidence; missing
values remain missing rather than being estimated from mutable price tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # Keep the module importable in dependency-light evaluator environments.
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # pragma: no cover - exercised only without LangChain

    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
_TRACE_KEYS = (
    "root_trace_id",
    "run_manifest_id",
    "incident_id",
    "job_id",
)


class ModelAccountingError(RuntimeError):
    """A model could not be instrumented without losing accounting coverage."""


@dataclass(frozen=True)
class RoutedModelIdentity:
    task_type: str
    tier: str
    requested_provider: str
    requested_model: Optional[str]
    constructed_provider: Optional[str]
    constructed_model: Optional[str]
    fallback_allowed: bool


@dataclass
class _PendingCall:
    run_id: str
    parent_run_id: Optional[str]
    started_at: str
    started_monotonic: float
    trace: dict[str, Optional[str]]
    actual_provider: Optional[str]
    actual_model: Optional[str]


def _artifact_path() -> Path:
    configured = os.getenv("MODEL_ACCOUNTING_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path("reports/model-accounting.jsonl")


def _clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _provider_from_model(model: Any) -> Optional[str]:
    module = type(model).__module__.lower()
    name = type(model).__name__.lower()
    combined = f"{module}.{name}"
    for marker, provider in (
        ("litellm", "litellm"),
        ("groq", "groq"),
        ("anthropic", "anthropic"),
        ("ollama", "ollama"),
        ("openai", "openai_compatible"),
    ):
        if marker in combined:
            return provider
    return _clean_string(getattr(model, "provider", None))


def _model_from_instance(model: Any) -> Optional[str]:
    for attribute in ("model_name", "model", "model_id"):
        value = _clean_string(getattr(model, attribute, None))
        if value:
            return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _response_mappings(response: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    llm_output = _mapping(getattr(response, "llm_output", None))
    if llm_output:
        values.append(llm_output)
        for key in ("token_usage", "usage", "usage_metadata", "_hidden_params"):
            nested = _mapping(llm_output.get(key))
            if nested:
                values.append(nested)
    for generation_list in getattr(response, "generations", None) or []:
        for generation in generation_list or []:
            message = getattr(generation, "message", None)
            for attribute in ("usage_metadata", "response_metadata"):
                nested = _mapping(getattr(message, attribute, None))
                if nested:
                    values.append(nested)
                    for key in ("token_usage", "usage", "_hidden_params"):
                        child = _mapping(nested.get(key))
                        if child:
                            values.append(child)
    return values


def _first_number(
    mappings: list[dict[str, Any]], keys: tuple[str, ...]
) -> Optional[float]:
    for mapping in mappings:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value >= 0:
                    return float(value)
    return None


def _first_string(
    mappings: list[dict[str, Any]], keys: tuple[str, ...]
) -> Optional[str]:
    for mapping in mappings:
        for key in keys:
            value = _clean_string(mapping.get(key))
            if value:
                return value
    return None


def _usage(response: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    mappings = _response_mappings(response)
    input_value = _first_number(mappings, ("input_tokens", "prompt_tokens"))
    output_value = _first_number(mappings, ("output_tokens", "completion_tokens"))
    total_value = _first_number(mappings, ("total_tokens",))
    input_tokens = int(input_value) if input_value is not None else None
    output_tokens = int(output_value) if output_value is not None else None
    total_tokens = int(total_value) if total_value is not None else None
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _cost(response: Any) -> Optional[float]:
    return _first_number(
        _response_mappings(response),
        ("response_cost", "cost_usd", "total_cost", "cost"),
    )


def _actual_route(
    response: Any,
    default_provider: Optional[str],
    default_model: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    mappings = _response_mappings(response)
    provider = _first_string(mappings, ("model_provider", "provider", "provider_name"))
    model = _first_string(mappings, ("model_name", "model", "model_id"))
    if not provider and model and "/" in model:
        provider = model.split("/", 1)[0]
    return provider or default_provider, model or default_model


class ModelAccountingRecorder:
    """Thread-safe in-process index backed by an append-only JSONL artifact."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._finalized: dict[str, dict[str, Optional[str]]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._records = []
            self._finalized = {}

    def _append_artifact(self, value: dict[str, Any]) -> Optional[str]:
        path = _artifact_path()
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)
        except OSError as exc:
            logger.error("Model accounting artifact write failed: %s", exc)
            return f"artifact_write_failed:{type(exc).__name__}"
        return None

    def record(self, value: dict[str, Any]) -> None:
        write_error = self._append_artifact(value)
        if write_error:
            value["completeness_reasons"].append(write_error)
        with self._lock:
            self._records.append(value)

    def finalize_trace(
        self,
        root_trace_id: str,
        *,
        status: str,
        reason: Optional[str] = None,
    ) -> None:
        trace_id = _clean_string(root_trace_id)
        if not trace_id:
            raise ModelAccountingError(
                "root_trace_id is required to finalize accounting"
            )
        if status not in {"success", "error"}:
            raise ModelAccountingError("accounting finalization status is invalid")
        marker = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "trace_finalization",
            "root_trace_id": trace_id,
            "status": status,
            "reason": _clean_string(reason),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        write_error = self._append_artifact(marker)
        with self._lock:
            self._finalized[trace_id] = {
                "status": status,
                "reason": _clean_string(reason),
                "write_error": write_error,
            }

    def records(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(record) for record in self._records)

    def summary(
        self,
        *,
        incident_id: Optional[str] = None,
        root_trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        records = [
            record
            for record in self.records()
            if (incident_id is None or record["trace"]["incident_id"] == incident_id)
            and (
                root_trace_id is None
                or record["trace"]["root_trace_id"] == root_trace_id
            )
        ]
        if not records:
            return {
                "schema_version": SCHEMA_VERSION,
                "complete": False,
                "completeness_reasons": ["no_model_calls_recorded"],
                "calls": 0,
                "cost_usd": None,
                "tokens": None,
                "latency_ms": 0.0,
                "fallbacks": [],
                "root_trace_ids": [],
                "records_sha256": None,
                "artifact_path": str(_artifact_path()),
            }
        reasons = sorted(
            {
                f"{record['call_id']}:{reason}"
                for record in records
                for reason in record["completeness_reasons"]
            }
        )
        trace_ids = tuple(
            dict.fromkeys(
                record["trace"]["root_trace_id"]
                for record in records
                if record["trace"]["root_trace_id"]
            )
        )
        with self._lock:
            finalizations = {
                trace_id: dict(self._finalized.get(trace_id, {}))
                for trace_id in trace_ids
            }
        for trace_id, finalization in finalizations.items():
            if not finalization:
                reasons.append(f"{trace_id}:trace_not_finalized")
                continue
            if finalization.get("status") != "success":
                reasons.append(f"{trace_id}:trace_failed")
            if finalization.get("write_error"):
                reasons.append(f"{trace_id}:finalization_{finalization['write_error']}")
        reasons = sorted(set(reasons))
        complete = not reasons
        fallbacks = [
            {
                "call_id": record["call_id"],
                "from": record["routing"]["fallback_from"],
                "to_provider": record["routing"]["actual_provider"],
                "to_model": record["routing"]["actual_model"],
            }
            for record in records
            if record["routing"]["fallback_from"]
        ]
        canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
        return {
            "schema_version": SCHEMA_VERSION,
            "complete": complete,
            "completeness_reasons": reasons,
            "calls": len(records),
            "cost_usd": (
                round(sum(record["cost_usd"] for record in records), 12)
                if complete
                else None
            ),
            "tokens": (
                {
                    "input": sum(record["tokens"]["input"] for record in records),
                    "output": sum(record["tokens"]["output"] for record in records),
                    "total": sum(record["tokens"]["total"] for record in records),
                }
                if complete
                else None
            ),
            "latency_ms": round(sum(record["latency_ms"] for record in records), 3),
            "fallbacks": fallbacks,
            "root_trace_ids": list(trace_ids),
            "records_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "artifact_path": str(_artifact_path()),
        }


_RECORDER = ModelAccountingRecorder()


def get_model_accounting_recorder() -> ModelAccountingRecorder:
    return _RECORDER


class ModelAccountingCallback(BaseCallbackHandler):
    """LangChain callback that records only call metadata and provider evidence."""

    def __init__(self, identity: RoutedModelIdentity) -> None:
        super().__init__()
        self.identity = identity
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.Lock()

    def _start(
        self,
        run_id: Any,
        parent_run_id: Any,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        identifier = str(run_id)
        values = _mapping(metadata)
        trace = {key: _clean_string(values.get(key)) for key in _TRACE_KEYS}
        actual_provider = (
            _clean_string(values.get("ls_provider"))
            or self.identity.constructed_provider
        )
        actual_model = (
            _clean_string(values.get("ls_model_name"))
            or self.identity.constructed_model
        )
        with self._lock:
            self._pending.setdefault(
                identifier,
                _PendingCall(
                    run_id=identifier,
                    parent_run_id=_clean_string(parent_run_id),
                    started_at=datetime.now(timezone.utc).isoformat(),
                    started_monotonic=time.perf_counter(),
                    trace=trace,
                    actual_provider=actual_provider,
                    actual_model=actual_model,
                ),
            )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        self._start(run_id, parent_run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        del serialized, prompts, kwargs
        self._start(run_id, parent_run_id, metadata)

    def _finish(
        self, run_id: Any, response: Any, error: Optional[BaseException]
    ) -> None:
        identifier = str(run_id)
        with self._lock:
            pending = self._pending.pop(identifier, None)
        if pending is None:
            pending = _PendingCall(
                run_id=identifier,
                parent_run_id=None,
                started_at=datetime.now(timezone.utc).isoformat(),
                started_monotonic=time.perf_counter(),
                trace={key: None for key in _TRACE_KEYS},
                actual_provider=self.identity.constructed_provider,
                actual_model=self.identity.constructed_model,
            )
        input_tokens, output_tokens, total_tokens = _usage(response)
        actual_provider, actual_model = _actual_route(
            response,
            pending.actual_provider,
            pending.actual_model,
        )
        cost_usd = _cost(response)
        reasons = [
            f"{key}_missing" for key, value in pending.trace.items() if not value
        ]
        for field, value in (
            ("actual_provider", actual_provider),
            ("actual_model", actual_model),
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("total_tokens", total_tokens),
            ("cost", cost_usd),
        ):
            if value is None:
                reasons.append(f"{field}_unavailable")
        if error is not None:
            reasons.append("call_failed")
        requested_model = self.identity.requested_model
        fallback = None
        if actual_provider and actual_provider != self.identity.requested_provider:
            fallback = self.identity.requested_provider
        elif requested_model and actual_model and actual_model != requested_model:
            fallback = f"{self.identity.requested_provider}/{requested_model}"
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "model_call",
            "call_id": pending.run_id,
            "parent_call_id": pending.parent_run_id,
            "started_at": pending.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "task_type": self.identity.task_type,
            "tier": self.identity.tier,
            "routing": {
                "requested_provider": self.identity.requested_provider,
                "requested_model": requested_model,
                "actual_provider": actual_provider,
                "actual_model": actual_model,
                "fallback_allowed": self.identity.fallback_allowed,
                "fallback_from": fallback,
            },
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens,
            },
            "cost_usd": cost_usd,
            "latency_ms": round(
                (time.perf_counter() - pending.started_monotonic) * 1000.0, 3
            ),
            "status": "error" if error is not None else "success",
            "error_type": type(error).__name__ if error is not None else None,
            "trace": pending.trace,
            "completeness_reasons": sorted(set(reasons)),
        }
        get_model_accounting_recorder().record(record)
        if pending.trace.get("root_trace_id"):
            try:
                from .trace_evidence import get_run_trace_recorder
            except ImportError:
                from sre_agent.trace_evidence import get_run_trace_recorder

            get_run_trace_recorder().record_span(
                root_trace_id=pending.trace["root_trace_id"],
                span_kind="model",
                name=f"{self.identity.task_type} model call",
                status="error" if error is not None else "success",
                span_id=pending.run_id,
                parent_span_id=pending.parent_run_id,
                started_at=pending.started_at,
                duration_ms=record["latency_ms"],
                attributes={
                    "gen_ai.provider.name": actual_provider,
                    "gen_ai.request.model": requested_model,
                    "gen_ai.response.model": actual_model,
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                    "sentinel.usage.total_tokens": total_tokens,
                    "sentinel.cost.usd": cost_usd,
                    "sentinel.task.type": self.identity.task_type,
                    "sentinel.model.tier": self.identity.tier,
                },
            )

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, response, None)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, None, error)


def instrument_llm(
    llm: Any,
    *,
    task_type: str,
    tier: str,
    requested_provider: str,
    requested_model: Optional[str],
    fallback_allowed: bool,
) -> Any:
    """Attach required accounting to a model without changing its interface."""
    constructed_provider = _provider_from_model(llm)
    constructed_model = _model_from_instance(llm)
    callback = ModelAccountingCallback(
        RoutedModelIdentity(
            task_type=str(task_type),
            tier=str(tier),
            requested_provider=requested_provider,
            requested_model=requested_model or constructed_model,
            constructed_provider=constructed_provider,
            constructed_model=constructed_model,
            fallback_allowed=fallback_allowed,
        )
    )
    try:
        existing = getattr(llm, "callbacks", None)
        if hasattr(existing, "add_handler"):
            existing.add_handler(callback)
            return llm
        callbacks = list(existing) if isinstance(existing, (list, tuple)) else []
        callbacks.append(callback)
        setattr(llm, "callbacks", callbacks)
    except Exception as exc:
        raise ModelAccountingError(
            f"model accounting callback could not be attached: {type(exc).__name__}"
        ) from exc
    return llm
