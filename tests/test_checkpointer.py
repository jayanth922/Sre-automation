#!/usr/bin/env python3
"""Unit tests for durable checkpointing config (interview Q2)."""

import importlib.util
import asyncio
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "checkpointer.py"
_spec = importlib.util.spec_from_file_location("checkpointer", _MODULE_PATH)
checkpointer = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checkpointer
_spec.loader.exec_module(checkpointer)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CHECKPOINTER_ENABLED", "CHECKPOINTER_BACKEND"):
        monkeypatch.delenv(k, raising=False)


def test_disabled_returns_none():
    assert asyncio.run(checkpointer.get_checkpointer()) is None


def test_disabled_thread_config_passes_base_through():
    base = {"callbacks": ["x"]}
    assert checkpointer.thread_config("inc-1", base) is base
    assert checkpointer.thread_config("inc-1", None) is None


def test_enabled_memory_returns_saver(monkeypatch):
    pytest.importorskip("langgraph")
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    saver = asyncio.run(checkpointer.get_checkpointer())
    assert saver is not None
    assert saver.__class__.__name__ in ("MemorySaver", "InMemorySaver")


def test_enabled_thread_config_injects_thread_id(monkeypatch):
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    cfg = checkpointer.thread_config("inc-42", {"callbacks": ["h"]})
    assert cfg["configurable"]["thread_id"] == "inc-42"
    assert cfg["callbacks"] == ["h"]  # base preserved


def test_enabled_thread_config_from_none_base(monkeypatch):
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    cfg = checkpointer.thread_config("inc-7")
    assert cfg == {"configurable": {"thread_id": "inc-7"}}


def test_select_backend_default_and_override(monkeypatch):
    assert checkpointer.select_backend() == "memory"
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "Redis")
    assert checkpointer.select_backend() == "redis"


def test_only_external_backend_is_restart_durable(monkeypatch):
    assert checkpointer.durable_checkpointer_configured() is False
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    assert checkpointer.durable_checkpointer_configured() is False
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    assert checkpointer.durable_checkpointer_configured() is True


def test_api_runtime_refuses_external_backend_memory_fallback(monkeypatch):
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_MODE", "api")

    async def unavailable():
        raise OSError("database unavailable")

    monkeypatch.setattr(checkpointer, "_postgres_saver", unavailable)
    with pytest.raises(RuntimeError, match="durable checkpointer is unavailable"):
        asyncio.run(checkpointer.get_checkpointer())


def test_thread_id_from_state_prefers_incident_id():
    assert checkpointer.thread_id_from_state({"incident_id": "inc-1", "session_id": "s"}) == "inc-1"
    assert checkpointer.thread_id_from_state({"metadata": {"incident_id": "inc-2"}}) == "inc-2"
    assert checkpointer.thread_id_from_state({"session_id": "s-3"}) == "s-3"
    assert checkpointer.thread_id_from_state({}) == "adhoc"
    assert checkpointer.thread_id_from_state(None) == "adhoc"


def test_agent_runtime_uses_configured_checkpointer():
    """`initialize_agent()` must build its checkpointer via `get_checkpointer()`
    (so CHECKPOINTER_BACKEND=redis/postgres survives an API restart), not a
    hardcoded `MemorySaver()` — a full API restart would otherwise silently
    drop any pending human-approval interrupt.

    Source-inspection only (no import): `agent_runtime.py` transitively pulls
    in fastapi/sqlalchemy/backend.database, which aren't available in this
    lightweight test environment.
    """
    src = (Path(__file__).resolve().parents[1] / "sre_agent" / "agent_runtime.py").read_text()
    assert "from .checkpointer import get_checkpointer" in src
    assert "checkpointer = await get_checkpointer()" in src
    assert "from langgraph.checkpoint.memory import MemorySaver" not in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
