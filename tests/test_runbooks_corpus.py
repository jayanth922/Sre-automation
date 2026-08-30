#!/usr/bin/env python3
"""Tests for shared runbook corpus path resolution (R11)."""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_corpus = _load("runbooks_corpus_under_test", "sre_agent/runbooks_corpus.py")


def test_resolve_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNBOOKS_DIR", str(tmp_path))
    path = _corpus.resolve_runbooks_dir(create=True)
    assert path == tmp_path.resolve()
    assert path.is_dir()


def test_default_matches_mcp_corpus(monkeypatch):
    monkeypatch.delenv("RUNBOOKS_DIR", raising=False)
    path = _corpus.default_runbooks_dir()
    assert path.name == "runbooks"
    assert "runbooks_local" in str(path)


def test_generator_writes_shared_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNBOOKS_DIR", str(tmp_path))
    # Reload generator after env is set so runbooks_dir() sees it.
    gen = _load("runbook_generator_r11_test", "sre_agent/runbook_generator.py")
    inp = gen.RunbookInput(
        alert_name="HighErrorRate",
        service="checkout-service",
        failure_class="high_error_rate",
        severity="SEV2",
        severity_label="critical",
        hypothesis="bad deploy",
        confidence=0.9,
        actions=[{"action_type": "rollback", "target": "checkout-service"}],
        namespace="demo",
        incident_id="inc-1",
        verification_status="RESOLVED",
    )
    written = gen.write_runbook(inp, target_dir=tmp_path)
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "agent_retrievable: true" in text or "agent_retrievable: True" in text
    assert "Auto-generated" in text
    # Same resolve path the API uses.
    assert _corpus.resolve_runbooks_dir(create=False) == tmp_path.resolve()
