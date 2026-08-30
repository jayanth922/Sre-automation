#!/usr/bin/env python3
"""Prove there is a single production incident investigation entry point."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sre_agent.incident_runner import CANONICAL_ENTRYPOINT, run_incident_investigation

ROOT = Path(__file__).resolve().parents[1]
DIRECT_RUNNER_CALLERS = [ROOT / "sre_agent" / "job_worker.py"]


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_canonical_entrypoint_constant():
    assert (
        CANONICAL_ENTRYPOINT == "sre_agent.incident_runner.run_incident_investigation"
    )
    assert callable(run_incident_investigation)


def test_production_api_callers_use_canonical_runner_only():
    for path in DIRECT_RUNNER_CALLERS:
        imported = _imported_names(path)
        source = path.read_text()
        assert "sre_agent.incident_runner.run_incident_investigation" in imported, path
        assert "sre_agent.agent_runtime.run_graph_background_saas" not in imported, path
        assert "sre_agent.agent_runtime_tasks" not in source, path
        assert "run_incident_investigation" in source, path


def test_mission_control_routes_investigations_through_durable_worker():
    path = ROOT / "sre_agent" / "api" / "v1" / "mission_control.py"
    imported = _imported_names(path)
    assert "sre_agent.job_worker.enqueue_and_kick" in imported
    assert "sre_agent.agent_runtime.run_graph_background_saas" not in imported


def test_alternate_runner_is_quarantined_forwarder():
    source = (ROOT / "sre_agent" / "agent_runtime_tasks.py").read_text()
    assert "QUARANTINED" in source
    assert "run_incident_investigation" in source
    assert "agent_graph.astream" not in source
    assert "initialize_agent" not in source


def test_no_second_production_graph_runner_module():
    """Only the canonical facade + agent_runtime implementation may invoke astream for SaaS."""
    tasks = ROOT / "sre_agent" / "agent_runtime_tasks.py"
    assert "astream" not in tasks.read_text()

    # Documentation pointer
    runner_doc = (ROOT / "sre_agent" / "incident_runner.py").read_text()
    assert "Canonical incident investigation runner" in runner_doc
    assert "run_incident_investigation" in runner_doc


@pytest.mark.asyncio
async def test_quarantined_tasks_forward_to_canonical(monkeypatch):
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "ok"

    from sre_agent import agent_runtime_tasks

    monkeypatch.setattr(agent_runtime_tasks, "run_incident_investigation", fake_run)

    with pytest.warns(DeprecationWarning, match="quarantined"):
        result = await agent_runtime_tasks.run_graph_background_saas(
            incident_id="inc",
            cluster_id="cluster",
            alert_name="HighCPU",
            job_id="job",
        )
    assert result == "ok"
    assert calls == [
        {
            "incident_id": "inc",
            "cluster_id": "cluster",
            "alert_name": "HighCPU",
            "job_id": "job",
        }
    ]


@pytest.mark.asyncio
async def test_canonical_runner_forwards_durable_lease_scope(monkeypatch):
    from sre_agent import agent_runtime

    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(agent_runtime, "run_graph_background_saas", fake_run)
    result = await run_incident_investigation(
        incident_id="inc",
        cluster_id="cluster",
        alert_name="HighCPU",
        organization_id="org",
        admission_owner="worker-1",
    )

    assert result == "ok"
    assert captured["organization_id"] == "org"
    assert captured["admission_owner"] == "worker-1"
