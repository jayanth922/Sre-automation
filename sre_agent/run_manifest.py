"""Immutable, secret-free provenance for one incident-agent run.

The manifest captures the configuration required to decide whether two runs
are comparable. Dynamic payloads are sanitized and hashed; secrets and raw tool
I/O never enter this record. Detailed tool events remain trace artifacts linked
by ``root_trace_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import models
from sre_agent.constants import SREConstants
from sre_agent.litellm_backend import litellm_enabled, tier_litellm_model
from sre_agent.model_router import TaskType, select_model
from sre_agent.prompt_loader import PromptLoader, prompt_loader

MANIFEST_SCHEMA_VERSION = 1
GRAPH_SCHEMA_VERSION = "1"
TOOL_SCHEMA_VERSION = "1"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
_CONFIG_SECTIONS = ("provenance", "models", "tools", "runtime")


class RunManifestImmutableError(RuntimeError):
    """Raised when a caller tries to replace an existing job manifest."""


@dataclass(frozen=True)
class BuiltRunManifest:
    data: dict[str, Any]
    sha256: str
    comparable: bool
    non_comparable_reasons: tuple[str, ...]
    root_trace_id: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value[:2048]
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))[:2048]
    except Exception:
        return "[invalid-url]"


def sanitize_for_manifest(value: Any, *, key: str = "") -> Any:
    """Convert arbitrary input to bounded JSON without retaining secret values."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "url" in key.lower() or "endpoint" in key.lower():
            return _safe_url(value)
        return value[:2048]
    if isinstance(value, Mapping):
        return {
            str(item_key)[:128]: sanitize_for_manifest(item_value, key=str(item_key))
            for item_key, item_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        }
    if isinstance(value, (set, frozenset)):
        return [
            sanitize_for_manifest(item, key=key)
            for item in sorted(value, key=lambda item: str(item))[:256]
        ]
    if isinstance(value, (list, tuple)):
        return [sanitize_for_manifest(item, key=key) for item in list(value)[:256]]
    return str(value)[:2048]


def _resolve_code_sha(explicit: Optional[str] = None) -> Optional[str]:
    candidates = (
        explicit,
        os.getenv("SENTINEL_CODE_SHA"),
        os.getenv("GIT_COMMIT"),
        os.getenv("SOURCE_VERSION"),
    )
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized and normalized not in {"unknown", "unset", "none"}:
            return normalized
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip().lower() or None
    except Exception:
        return None


def _working_tree_dirty(explicit_code_sha: Optional[str]) -> Optional[bool]:
    if explicit_code_sha or any(
        os.getenv(name)
        for name in ("SENTINEL_CODE_SHA", "GIT_COMMIT", "SOURCE_VERSION")
    ):
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return bool(result.stdout.strip())
    except Exception:
        return None


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _model_routes(provider: str) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for task_type in TaskType:
        decision = select_model(task_type, provider=provider)
        litellm_model = (
            tier_litellm_model(decision.tier.value) if litellm_enabled() else None
        )
        if litellm_model:
            model_id = litellm_model
            effective_provider = "litellm"
            max_tokens = SREConstants.model.default_max_tokens
        else:
            model_kwargs: dict[str, Any] = {}
            if decision.model_id:
                model_kwargs["model_id"] = decision.model_id
            try:
                config = SREConstants.get_model_config(
                    decision.provider, **model_kwargs
                )
                model_id = config.get("model_id")
                max_tokens = config.get("max_tokens")
            except ValueError:
                model_id = decision.model_id
                max_tokens = None
            effective_provider = decision.provider
        routes.append(
            {
                "task_type": task_type.value,
                "tier": decision.tier.value,
                "provider": effective_provider,
                "model_id": model_id,
                "temperature": decision.temperature,
                "max_tokens": max_tokens,
                "reasoning_effort": os.getenv("LLM_REASONING_EFFORT"),
                "seed": os.getenv("LLM_SEED"),
                "router_reason": decision.reason,
            }
        )
    return routes


def _tool_schema(tool: Any) -> dict[str, Any]:
    name = str(
        getattr(tool, "name", None) or getattr(tool, "__name__", type(tool).__name__)
    )
    description = str(getattr(tool, "description", "") or "")
    args_schema = getattr(tool, "args_schema", None)
    schema: Any = None
    try:
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            schema = args_schema.model_json_schema()
        elif args_schema is not None and hasattr(args_schema, "schema"):
            schema = args_schema.schema()
        elif hasattr(tool, "get_input_schema"):
            input_schema = tool.get_input_schema()
            schema = input_schema.model_json_schema()
    except Exception:
        schema = None
    safe_schema = sanitize_for_manifest(schema or {})
    return {
        "name": name,
        "version": str(getattr(tool, "version", TOOL_SCHEMA_VERSION)),
        "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "schema_sha256": _sha256_json(safe_schema),
    }


def _tool_schemas(tools: Iterable[Any]) -> list[dict[str, Any]]:
    if isinstance(tools, Mapping):
        values = tools.values()
    else:
        values = tools
    return sorted(
        (_tool_schema(tool) for tool in values), key=lambda item: item["name"]
    )


def build_run_manifest(
    *,
    execution_context: Any,
    tools: Iterable[Any],
    incident_id: uuid.UUID | str,
    job_id: uuid.UUID | str,
    input_payload: Mapping[str, Any],
    root_trace_id: Optional[str] = None,
    code_sha: Optional[str] = None,
    prompts: Optional[PromptLoader] = None,
    created_at: Optional[datetime] = None,
) -> BuiltRunManifest:
    """Build a deterministic, secret-free manifest and its comparability verdict."""
    trace_id = root_trace_id or uuid.uuid4().hex
    timestamp = (created_at or datetime.now(timezone.utc)).isoformat()
    resolved_code_sha = _resolve_code_sha(code_sha)
    working_tree_dirty = _working_tree_dirty(code_sha)
    prompt_set = (prompts or prompt_loader).prompt_fingerprints()
    tool_set = _tool_schemas(tools)
    graph_sha = _file_sha256(_REPO_ROOT / "sre_agent" / "graph_builder.py")
    provider = str(
        getattr(execution_context, "llm_provider", None)
        or os.getenv("LLM_PROVIDER", "anthropic")
    ).lower()
    sanitized_input = sanitize_for_manifest(input_payload)
    model_routes = _model_routes(provider)

    data: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run": {
            "job_id": str(job_id),
            "incident_id": str(incident_id),
            "created_at": timestamp,
        },
        "tenant": {
            "organization_id": str(
                getattr(execution_context, "organization_id", "") or ""
            ),
            "cluster_id": str(getattr(execution_context, "cluster_id", "") or ""),
            "namespace": getattr(execution_context, "namespace", None),
            "environment": getattr(execution_context, "environment", "production"),
            "context_version": getattr(execution_context, "context_version", None),
        },
        "provenance": {
            "code_sha": resolved_code_sha,
            "working_tree_dirty": working_tree_dirty,
            "graph": {
                "version": os.getenv("SENTINEL_GRAPH_VERSION", GRAPH_SCHEMA_VERSION),
                "sha256": graph_sha,
            },
            "prompts": {
                "version": os.getenv("SENTINEL_PROMPT_VERSION", "sha256-v1"),
                "files": prompt_set,
            },
            "dataset": {
                "version": os.getenv("SENTINEL_DATASET_VERSION", "live-input/v1"),
                "fixture_version": os.getenv("SENTINEL_FIXTURE_VERSION"),
            },
        },
        "models": {
            "requested_cluster_model": getattr(execution_context, "llm_model", None),
            "routes": model_routes,
            "fallback_chain": [provider]
            + [item for item in ("anthropic", "gemini") if item != provider],
        },
        "tools": {
            "schema_version": os.getenv(
                "SENTINEL_TOOL_SCHEMA_VERSION", TOOL_SCHEMA_VERSION
            ),
            "schemas": tool_set,
            "io_reference": {
                "uri": f"trace://{trace_id}/tool-io",
                "capture": (
                    "redacted"
                    if os.getenv("TRACE_PAYLOAD_CAPTURE", "").lower()
                    in {"1", "true", "yes"}
                    else "metadata-only"
                ),
            },
        },
        "runtime": {
            "checkpointer_backend": os.getenv("CHECKPOINTER_BACKEND", "memory"),
            "model_router_enabled": os.getenv("MODEL_ROUTER_ENABLED", "true").lower(),
            "model_router_backend": os.getenv("MODEL_ROUTER_BACKEND", "provider"),
            "executor_live": os.getenv("EXECUTOR_LIVE", "false").lower(),
            "act_phase_enabled": os.getenv("ACT_PHASE_ENABLED", "false").lower(),
        },
        "input": {
            "sha256": _sha256_json(sanitized_input),
            "sanitized": sanitized_input,
        },
        "trace": {"root_trace_id": trace_id},
    }

    reasons: list[str] = []
    if not resolved_code_sha:
        reasons.append("missing provenance.code_sha")
    if working_tree_dirty:
        reasons.append("provenance working tree has uncommitted changes")
    if not graph_sha:
        reasons.append("missing provenance.graph.sha256")
    if not prompt_set:
        reasons.append("missing provenance.prompts.files")
    if not tool_set:
        reasons.append("missing tools.schemas")
    if not data["tenant"]["organization_id"] or not data["tenant"]["cluster_id"]:
        reasons.append("missing tenant identity")
    for route in model_routes:
        if not route.get("provider") or not route.get("model_id"):
            reasons.append(f"missing model provenance for {route['task_type']}")

    digest = _sha256_json(data)
    return BuiltRunManifest(
        data=data,
        sha256=digest,
        comparable=not reasons,
        non_comparable_reasons=tuple(reasons),
        root_trace_id=trace_id,
    )


async def persist_run_manifest(
    db: AsyncSession,
    *,
    built: BuiltRunManifest,
    job_id: uuid.UUID,
    incident_id: uuid.UUID,
    cluster_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> models.RunManifest:
    """Insert once per job; retries are idempotent and replacements are rejected."""
    existing = await db.scalar(
        select(models.RunManifest).where(models.RunManifest.job_id == job_id)
    )
    if existing is not None:
        if existing.manifest_sha256 != built.sha256:
            raise RunManifestImmutableError(
                f"run manifest for job {job_id} already exists with a different digest"
            )
        return existing

    row = models.RunManifest(
        job_id=job_id,
        incident_id=incident_id,
        cluster_id=cluster_id,
        organization_id=organization_id,
        schema_version=MANIFEST_SCHEMA_VERSION,
        manifest=built.data,
        manifest_sha256=built.sha256,
        comparable=built.comparable,
        non_comparable_reasons=list(built.non_comparable_reasons),
        root_trace_id=built.root_trace_id,
    )
    db.add(row)
    await db.flush()
    return row


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flattened: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(value[key], child))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, item in enumerate(value):
            child = f"{prefix}[{index}]"
            flattened.update(_flatten(item, child))
        return flattened
    return {prefix: value}


def _differences(left: Any, right: Any) -> list[dict[str, Any]]:
    left_flat = _flatten(left)
    right_flat = _flatten(right)
    return [
        {"path": path, "left": left_flat.get(path), "right": right_flat.get(path)}
        for path in sorted(set(left_flat) | set(right_flat))
        if left_flat.get(path) != right_flat.get(path)
    ]


def compare_run_manifests(
    left: models.RunManifest, right: models.RunManifest
) -> dict[str, Any]:
    """Report exact configuration and input drift between two persisted runs."""
    left_config = {key: left.manifest.get(key) for key in _CONFIG_SECTIONS}
    right_config = {key: right.manifest.get(key) for key in _CONFIG_SECTIONS}
    config_differences = _differences(left_config, right_config)
    input_differences = _differences(
        left.manifest.get("input"), right.manifest.get("input")
    )
    reasons = sorted(
        set(left.non_comparable_reasons or []) | set(right.non_comparable_reasons or [])
    )
    return {
        "left_job_id": str(left.job_id),
        "right_job_id": str(right.job_id),
        "comparable": bool(left.comparable and right.comparable),
        "non_comparable_reasons": reasons,
        "configuration_equal": not config_differences,
        "configuration_differences": config_differences,
        "input_differences": input_differences,
    }
