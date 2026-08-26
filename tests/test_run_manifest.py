"""A01 run-manifest provenance, immutability, and comparison tests."""

from __future__ import annotations

import json
import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sre_agent.prompt_loader import PromptLoader
from sre_agent.run_manifest import (
    RunManifestImmutableError,
    build_run_manifest,
    compare_run_manifests,
    persist_run_manifest,
    sanitize_for_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


class _ArgsSchema:
    @classmethod
    def model_json_schema(cls):
        return {
            "type": "object",
            "properties": {"namespace": {"type": "string"}},
            "required": ["namespace"],
        }


class _Tool:
    name = "query_metrics"
    description = "Read scoped metrics"
    args_schema = _ArgsSchema
    version = "metrics/v2"


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        organization_id=str(uuid.UUID(int=1)),
        cluster_id=str(uuid.UUID(int=2)),
        namespace="payments",
        environment="production",
        context_version=4,
        llm_provider="groq",
        llm_model=None,
        credentials={"llm_api_key": "never-store-me"},
    )


def _prompt_loader(tmp_path: Path) -> PromptLoader:
    (tmp_path / "agent_base_prompt.txt").write_text("  Base prompt\n", encoding="utf-8")
    (tmp_path / "logs_agent_prompt.txt").write_text("Logs prompt", encoding="utf-8")
    return PromptLoader(str(tmp_path))


def test_manifest_is_deterministic_tamper_evident_and_secret_free(tmp_path):
    kwargs = {
        "execution_context": _context(),
        "tools": [_Tool()],
        "incident_id": uuid.UUID(int=3),
        "job_id": uuid.UUID(int=4),
        "input_payload": {
            "labels": {"service": "payments", "api_token": "input-secret"},
            "endpoint": "https://user:pass@example.test/api?token=secret",
        },
        "root_trace_id": "trace-123",
        "code_sha": "0123456789abcdef",
        "prompts": _prompt_loader(tmp_path),
        "created_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
    }

    first = build_run_manifest(**kwargs)
    second = build_run_manifest(**kwargs)

    assert first.sha256 == second.sha256
    assert first.comparable is True
    assert first.data["provenance"]["code_sha"] == "0123456789abcdef"
    assert (
        first.data["provenance"]["prompts"]["files"]["agent_base_prompt"]["bytes"] == 11
    )
    assert first.data["tools"]["schemas"][0]["version"] == "metrics/v2"
    assert first.data["input"]["sanitized"]["labels"]["api_token"] == "[REDACTED]"
    assert first.data["input"]["sanitized"]["endpoint"] == "https://example.test/api"
    rendered = json.dumps(first.data)
    assert "input-secret" not in rendered
    assert "never-store-me" not in rendered
    assert "user:pass" not in rendered


def test_missing_required_provenance_marks_run_non_comparable(tmp_path, monkeypatch):
    import sre_agent.run_manifest as module

    monkeypatch.setattr(module, "_resolve_code_sha", lambda explicit=None: None)
    built = build_run_manifest(
        execution_context=_context(),
        tools=[],
        incident_id=uuid.UUID(int=3),
        job_id=uuid.UUID(int=4),
        input_payload={"alert": "test"},
        prompts=_prompt_loader(tmp_path),
    )

    assert built.comparable is False
    assert "missing provenance.code_sha" in built.non_comparable_reasons
    assert "missing tools.schemas" in built.non_comparable_reasons


def test_comparison_reports_exact_configuration_and_input_drift(tmp_path, monkeypatch):
    common = {
        "execution_context": _context(),
        "tools": [_Tool()],
        "incident_id": uuid.UUID(int=3),
        "root_trace_id": "trace-123",
        "prompts": _prompt_loader(tmp_path),
        "created_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
    }
    left_built = build_run_manifest(
        **common,
        job_id=uuid.UUID(int=4),
        input_payload={"alert": "left"},
        code_sha="aaaaaaaa",
    )
    monkeypatch.setenv("MODEL_ROUTER_FAST_MODEL", "different-fast-model")
    right_built = build_run_manifest(
        **common,
        job_id=uuid.UUID(int=5),
        input_payload={"alert": "right"},
        code_sha="bbbbbbbb",
    )
    left = SimpleNamespace(
        job_id=uuid.UUID(int=4),
        manifest=left_built.data,
        comparable=True,
        non_comparable_reasons=[],
    )
    right = SimpleNamespace(
        job_id=uuid.UUID(int=5),
        manifest=right_built.data,
        comparable=True,
        non_comparable_reasons=[],
    )

    comparison = compare_run_manifests(left, right)

    assert comparison["comparable"] is True
    assert comparison["configuration_equal"] is False
    paths = {item["path"] for item in comparison["configuration_differences"]}
    assert "provenance.code_sha" in paths
    assert any(path.startswith("models.routes") for path in paths)
    assert comparison["input_differences"]


def test_persist_refuses_replacement_of_existing_manifest():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(manifest_sha256="existing"))
    )
    built = SimpleNamespace(sha256="replacement")

    with pytest.raises(RunManifestImmutableError, match="different digest"):
        asyncio.run(
            persist_run_manifest(
                session,
                built=built,
                job_id=uuid.UUID(int=4),
                incident_id=uuid.UUID(int=3),
                cluster_id=uuid.UUID(int=2),
                organization_id=uuid.UUID(int=1),
            )
        )


def test_sanitizer_redacts_nested_credentials_and_bounds_values():
    sanitized = sanitize_for_manifest(
        {"nested": {"authorization": "Bearer secret"}, "message": "x" * 3000}
    )
    assert sanitized["nested"]["authorization"] == "[REDACTED]"
    assert len(sanitized["message"]) == 2048
    assert sanitize_for_manifest({"values": {"b", "a"}}) == {"values": ["a", "b"]}


def test_model_migration_runtime_and_api_wire_manifest_contract():
    model_source = (ROOT / "backend" / "models.py").read_text()
    migration_source = (
        ROOT / "backend" / "alembic" / "versions" / "d3e4f5a6b7c8_add_run_manifests.py"
    ).read_text()
    runtime_source = (ROOT / "sre_agent" / "agent_runtime.py").read_text()
    route_source = (ROOT / "sre_agent" / "api" / "v1" / "jobs.py").read_text()
    ci_source = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    docker_source = (ROOT / "platform" / "Dockerfile").read_text()
    compose_source = (ROOT / "platform" / "docker-compose.yaml").read_text()

    assert "class RunManifest" in model_source
    assert 'down_revision: Union[str, None] = "c2d3e4f5a6b7"' in migration_source
    assert "run_manifests_reject_update" in migration_source
    persist_index = runtime_source.index("persist_run_manifest(")
    assert runtime_source.index("agent_graph.astream(", persist_index) > persist_index
    assert "get_job_manifest" in route_source
    assert "compare_job_manifests" in route_source
    assert "cluster.org_id != user.org_id" in route_source
    assert '"run_stage": "startup_failed"' in runtime_source
    assert "uv run alembic upgrade head" in ci_source
    assert "ARG SENTINEL_CODE_SHA=unknown" in docker_source
    assert "SENTINEL_CODE_SHA: ${SENTINEL_CODE_SHA:-unknown}" in compose_source
