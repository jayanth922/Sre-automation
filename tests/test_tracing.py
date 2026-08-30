#!/usr/bin/env python3
"""Unit tests for Langfuse tracing wiring (competitive-audit upgrade)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "tracing.py"
_spec = importlib.util.spec_from_file_location("tracing", _MODULE_PATH)
tr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tr
_spec.loader.exec_module(tr)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LANGFUSE_TRACING", "LANGFUSE_PUBLIC_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_enabled_by_default():
    # Self-hosted Langfuse is wired by default in every deployment; no env
    # var is required to opt in.
    assert tr.langfuse_enabled() is True


def test_disabled_via_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    assert tr.langfuse_enabled() is False
    assert tr.get_langfuse_callback() is None


def test_enabled_by_public_key(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    assert tr.langfuse_enabled() is True


def test_tracing_callbacks_passthrough_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    base = {"callbacks": ["existing"]}
    assert tr.tracing_callbacks(base) is base
    assert tr.tracing_callbacks(None) is None


def test_tracing_callbacks_appends_handler_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setattr(tr, "get_langfuse_callback", lambda: "LF_HANDLER")
    cfg = tr.tracing_callbacks({"callbacks": ["existing"], "configurable": {"thread_id": "i1"}})
    assert cfg["callbacks"] == ["existing", "LF_HANDLER"]
    assert cfg["configurable"] == {"thread_id": "i1"}  # base preserved


def test_flush_is_safe_when_disabled():
    tr.flush()  # no exception


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
