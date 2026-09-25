"""Production UI build + deployment recovery contracts (no live cluster required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_dashboard_has_production_build_script():
    pkg = json.loads((ROOT / "apps" / "dashboard" / "package.json").read_text())
    assert "build" in pkg.get("scripts", {})
    assert pkg["scripts"]["build"] == "next build"
    assert (ROOT / "infra" / "local" / "Dockerfile.dashboard").is_file()


@pytest.mark.integration
def test_deployment_smoke_artifacts_exist():
    required = [
        ROOT / "infra" / "helm" / "sentinel" / "Chart.yaml",
        ROOT / "infra" / "k8s" / "kustomization.yaml",
        ROOT / "infra" / "terraform" / "main.tf",
        ROOT / "scripts" / "ci" / "check_helm_rbac.sh",
        ROOT / "scripts" / "ci" / "check_helm_ws.sh",
        ROOT / "infra" / "local" / "Dockerfile",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    assert missing == [], f"missing deployment smoke artifacts: {missing}"


@pytest.mark.integration
def test_ci_runs_backend_frontend_and_manifest_checks():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in ci
    assert "tsc --noEmit" in ci or "npm run build" in ci
    assert "check_helm_rbac.sh" in ci
    assert "Dockerfile.dashboard" in ci or "infra/local/Dockerfile" in ci


@pytest.mark.integration
def test_qdrant_client_and_server_versions_stay_compatible():
    version = "1.19.1"
    digest = "12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'"qdrant-client>={version},<1.20.0"' in pyproject

    manifests = [
        ROOT / "infra" / "local" / "docker-compose.yaml",
        ROOT / "infra" / "helm" / "sentinel" / "values.yaml",
        ROOT / "infra" / "k8s" / "datastores.yaml",
    ]
    for manifest in manifests:
        text = manifest.read_text()
        assert f"qdrant/qdrant:v{version}@sha256:{digest}" in text, manifest
        assert "qdrant/qdrant:latest" not in text, manifest


@pytest.mark.integration
def test_postgres_and_redis_images_are_version_and_digest_pinned():
    manifests = [
        ROOT / "infra" / "local" / "docker-compose.yaml",
        ROOT / "infra" / "helm" / "sentinel" / "values.yaml",
        ROOT / "infra" / "k8s" / "datastores.yaml",
    ]
    expected = (
        "postgres:15.19-alpine@sha256:"
        "a46e076249ce434e41203b8c1dadfaa025b9726331d72390df038385d6dc29cd",
        "redis:7.4.11-alpine@sha256:"
        "520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7",
    )
    for manifest in manifests:
        text = manifest.read_text()
        for image in expected:
            assert image in text, manifest


@pytest.mark.integration
def test_temporal_server_and_sdk_are_pinned():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "infra" / "local" / "Dockerfile").read_text()
    compose = (ROOT / "infra" / "local" / "docker-compose.yaml").read_text()

    assert '"temporalio>=1.32.0,<1.33.0"' in pyproject
    assert "uv sync --frozen --no-dev --extra temporal --extra anthropic" in dockerfile
    assert 'uv pip install --no-cache "temporalio' not in dockerfile
    assert (
        "temporalio/temporal:1.8.3@sha256:"
        "cea463d98a8d6def4420f903ea5c3fcd0d85c8d10fbcc2770a50c12fff2eb26d"
    ) in compose
    assert "temporalio/temporal:latest" not in compose


@pytest.mark.integration
def test_anthropic_runtime_dependency_is_locked():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "infra" / "local" / "Dockerfile").read_text()

    assert '"langchain-anthropic>=1.7.2,<1.8.0"' in pyproject
    assert "--extra anthropic" in dockerfile
    assert 'uv pip install --no-cache "langchain-anthropic' not in dockerfile


@pytest.mark.integration
def test_image_installer_matches_pinned_ci_uv_version():
    dockerfile = (ROOT / "infra" / "local" / "Dockerfile").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'UV_VERSION: "0.6.14"' in ci
    assert 'pip install --no-cache-dir "uv==0.6.14"' in dockerfile
    assert "pip install --no-cache-dir uv" not in dockerfile


@pytest.mark.integration
def test_runtime_base_image_is_digest_pinned():
    dockerfile = (ROOT / "infra" / "local" / "Dockerfile").read_text()
    assert (
        "FROM python:3.12-slim-bookworm@sha256:"
        "782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
    ) in dockerfile


@pytest.mark.integration
def test_the_reports_directory_survives_a_rebuild():
    """src/sre_agent/trace_evidence.py and src/sre_agent/model_accounting.py default to
    a relative "reports/..." path, and WORKDIR is /app. Without a volume there,
    the run-trace evidence the release gate reads and the per-call cost ledger
    live on the container filesystem, which `docker compose build` discards --
    losing exactly the before-and-after numbers a rebuild exists to produce.
    The worker runs the same image, so it has to append to the same ledger.
    """
    compose = (ROOT / "infra" / "local" / "docker-compose.yaml").read_text()

    assert compose.count("- reports_data:/app/reports") == 2
    assert "\n  reports_data:\n    driver: local\n" in compose

    for module in ("trace_evidence", "model_accounting"):
        source = (ROOT / "src" / "sre_agent" / f"{module}.py").read_text()
        assert 'Path("reports/' in source or '"reports/' in source
