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
