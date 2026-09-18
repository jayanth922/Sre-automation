"""Production UI build + deployment recovery contracts (no live cluster required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_dashboard_has_production_build_script():
    pkg = json.loads((ROOT / "dashboard" / "package.json").read_text())
    assert "build" in pkg.get("scripts", {})
    assert pkg["scripts"]["build"] == "next build"
    assert (ROOT / "platform" / "Dockerfile.dashboard").is_file()


@pytest.mark.integration
def test_deployment_smoke_artifacts_exist():
    required = [
        ROOT / "deploy" / "helm" / "sentinel" / "Chart.yaml",
        ROOT / "deploy" / "k8s" / "kustomization.yaml",
        ROOT / "deploy" / "terraform" / "main.tf",
        ROOT / "scripts" / "check_helm_rbac.sh",
        ROOT / "scripts" / "check_helm_ws.sh",
        ROOT / "platform" / "Dockerfile",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    assert missing == [], f"missing deployment smoke artifacts: {missing}"


@pytest.mark.integration
def test_ci_runs_backend_frontend_and_manifest_checks():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in ci
    assert "tsc --noEmit" in ci or "npm run build" in ci
    assert "check_helm_rbac.sh" in ci
    assert "Dockerfile.dashboard" in ci or "platform/Dockerfile" in ci


@pytest.mark.integration
def test_qdrant_client_and_server_versions_stay_compatible():
    version = "1.19.1"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'"qdrant-client>={version},<1.20.0"' in pyproject

    manifests = [
        ROOT / "platform" / "docker-compose.yaml",
        ROOT / "deploy" / "helm" / "sentinel" / "values.yaml",
        ROOT / "deploy" / "k8s" / "datastores.yaml",
    ]
    for manifest in manifests:
        text = manifest.read_text()
        assert f"qdrant/qdrant:v{version}" in text, manifest
        assert "qdrant/qdrant:latest" not in text, manifest


@pytest.mark.integration
def test_temporal_server_and_sdk_are_pinned():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "platform" / "Dockerfile").read_text()
    compose = (ROOT / "platform" / "docker-compose.yaml").read_text()

    assert '"temporalio>=1.32.0,<1.33.0"' in pyproject
    assert "uv sync --frozen --no-dev --extra temporal --extra anthropic" in dockerfile
    assert 'uv pip install --no-cache "temporalio' not in dockerfile
    assert "temporalio/temporal:1.8.3" in compose
    assert "temporalio/temporal:latest" not in compose


@pytest.mark.integration
def test_anthropic_runtime_dependency_is_locked():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "platform" / "Dockerfile").read_text()

    assert '"langchain-anthropic>=1.7.2,<1.8.0"' in pyproject
    assert "--extra anthropic" in dockerfile
    assert 'uv pip install --no-cache "langchain-anthropic' not in dockerfile


@pytest.mark.integration
def test_image_installer_matches_pinned_ci_uv_version():
    dockerfile = (ROOT / "platform" / "Dockerfile").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'UV_VERSION: "0.6.14"' in ci
    assert 'pip install --no-cache-dir "uv==0.6.14"' in dockerfile
    assert "pip install --no-cache-dir uv" not in dockerfile


@pytest.mark.integration
def test_runtime_base_image_is_digest_pinned():
    dockerfile = (ROOT / "platform" / "Dockerfile").read_text()
    assert (
        "FROM python:3.12-slim-bookworm@sha256:"
        "782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
    ) in dockerfile
