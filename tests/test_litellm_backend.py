#!/usr/bin/env python3
"""Unit tests for the LiteLLM router backend (competitive-audit upgrade)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "sre_agent" / "litellm_backend.py"
_spec = importlib.util.spec_from_file_location("litellm_backend", _MODULE_PATH)
lb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lb
_spec.loader.exec_module(lb)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("MODEL_ROUTER_") or k in ("LITELLM_ENABLED",):
            monkeypatch.delenv(k, raising=False)


def test_disabled_by_default():
    assert lb.litellm_enabled() is False


def test_enabled_via_backend_env(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_BACKEND", "litellm")
    assert lb.litellm_enabled() is True


def test_enabled_via_flag(monkeypatch):
    monkeypatch.setenv("LITELLM_ENABLED", "true")
    assert lb.litellm_enabled() is True


def test_tier_model_resolution(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_STRONG_LITELLM_MODEL", "gpt-4o")
    assert lb.tier_litellm_model("strong") == "gpt-4o"


def test_tier_model_falls_back_to_generic_model_env(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_FAST_MODEL", "gemini/gemini-2.0-flash")
    assert lb.tier_litellm_model("fast") == "gemini/gemini-2.0-flash"


def test_tier_model_none_when_unset():
    assert lb.tier_litellm_model("balanced") is None


def test_build_raises_clean_error_without_package():
    # langchain_community/ChatLiteLLM not installed here → clear install hint.
    with pytest.raises(RuntimeError, match="pip install litellm"):
        lb.build_litellm_llm("gpt-4o")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
