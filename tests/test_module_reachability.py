"""P10: module reachability and UI/backend contract drift checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_module_reachability_script_passes():
    script = ROOT / "scripts" / "check_module_reachability.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generative_course_is_archived_not_shipped():
    assert not (ROOT / "sre_agent" / "generative_course.py").exists()
    assert (ROOT / "archive" / "experimental" / "generative_course.py").exists()
    assert (ROOT / "archive" / "experimental" / "README.md").exists()


def test_module_owners_doc_exists():
    doc = ROOT / "docs" / "architecture" / "MODULE_OWNERS.md"
    text = doc.read_text()
    assert "Production entry points" in text
    assert "actor_runtime" in text
    assert "archive/experimental" in text


def test_orphaned_dashboard_components_removed():
    dash = ROOT / "dashboard" / "components" / "dashboard"
    assert not (dash / "AuditLogTable.tsx").exists()
    assert not (dash / "AgentStatus.tsx").exists()


def test_live_dashboard_pages_use_existing_cluster_apis():
    """Pages may call /clusters/{id}/audit and /health — both exist on clusters router."""
    clusters = (ROOT / "sre_agent" / "api" / "v1" / "clusters.py").read_text()
    assert '@router.get("/{cluster_id}/health")' in clusters
    assert '@router.get("/{cluster_id}/audit")' in clusters

    audit_page = (
        ROOT / "dashboard" / "app" / "(dashboard)" / "clusters" / "[id]" / "audit" / "page.tsx"
    ).read_text()
    assert "/audit" in audit_page

    # No leftover course/learning UI that the backend cannot serve.
    for path in (ROOT / "dashboard").rglob("*.tsx"):
        text = path.read_text()
        assert "generative_course" not in text
        assert "/learning-modules" not in text
