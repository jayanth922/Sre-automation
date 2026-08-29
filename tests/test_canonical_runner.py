#!/usr/bin/env python3
"""Prove there is a single production incident investigation entry point."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sre_agent.incident_runner import CANONICAL_ENTRYPOINT, run_incident_investigation

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_CALLERS = [
    ROOT / "sre_agent" / "api" / "v1" / "alerts.py",
    ROOT / "sre_agent" / "api" / "v1" / "incidents.py",
    ROOT / "sre_agent" / "api" / "v1" / "mission_control.py",
]


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
    for path in PRODUCTION_CALLERS:
        imported = _imported_names(path)
        source = path.read_text()
        assert "sre_agent.incident_runner.run_incident_investigation" in imported, path
        assert "sre_agent.agent_runtime.run_graph_background_saas" not in imported, path
        assert "sre_agent.agent_runtime_tasks" not in source, path
        assert "run_incident_investigation" in source, path


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
