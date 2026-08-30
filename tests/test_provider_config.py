#!/usr/bin/env python3
"""Tests for fail-closed LLM provider / startup validation (P01)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "provider_config_under_test", _ROOT / "sre_agent" / "provider_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["provider_config_under_test"] = module
    spec.loader.exec_module(module)
    return module


pc = _load()


def test_supported_providers_pass():
    for provider in ("anthropic", "gemini"):
        assert pc.require_supported_provider(provider) == provider


@pytest.mark.parametrize(
    "bad",
    ["groq", "ollama", "nvidia", "openai", "openai_compatible", "bogus", ""],
)
def test_unsupported_providers_fail_closed(bad):
    with pytest.raises(pc.ProviderConfigError) as exc:
        pc.require_supported_provider(bad)
    msg = str(exc.value).lower()
    assert "not supported" in msg or "unset" in msg or "unsupported" in msg or "removed" in msg
    if bad in ("groq", "ollama", "nvidia", "openai_compatible"):
        assert "anthropic" in msg or "gemini" in msg


def test_anthropic_requires_real_key():
    with pytest.raises(pc.ProviderConfigError, match="ANTHROPIC_API_KEY"):
        pc.validate_provider_credentials("anthropic", {"ANTHROPIC_API_KEY": "YOUR_KEY"})
    with pytest.raises(pc.ProviderConfigError, match="ANTHROPIC_API_KEY"):
        pc.validate_provider_credentials("anthropic", {"ANTHROPIC_API_KEY": ""})
    pc.validate_provider_credentials("anthropic", {"ANTHROPIC_API_KEY": "sk-ant-api-test-key"})


def test_gemini_requires_real_key():
    with pytest.raises(pc.ProviderConfigError, match="GOOGLE_API_KEY"):
        pc.validate_provider_credentials("gemini", {"GOOGLE_API_KEY": "YOUR_KEY"})
    with pytest.raises(pc.ProviderConfigError, match="GOOGLE_API_KEY"):
        pc.validate_provider_credentials("gemini", {"GOOGLE_API_KEY": ""})
    pc.validate_provider_credentials("gemini", {"GOOGLE_API_KEY": "AIzaSyTestKey"})


def test_validate_startup_config_happy_path():
    env = {
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
    }
    assert pc.validate_startup_config(env) == "anthropic"


def test_validate_startup_rejects_groq():
    env = {
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "groq",
        "GROQ_API_KEY": "gsk_test_key",
    }
    with pytest.raises(pc.ProviderConfigError, match="Groq support was removed"):
        pc.validate_startup_config(env)


def test_cli_exits_nonzero_on_invalid(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert pc.main([]) == 1


def test_cli_exits_zero_on_valid(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    assert pc.main([]) == 0


def test_runtime_no_longer_coerces_provider():
    """Source guard: agent_runtime must not silently default invalid providers."""
    source = (_ROOT / "sre_agent" / "agent_runtime.py").read_text(encoding="utf-8")
    assert "defaulting to 'groq'" not in source
    assert "require_supported_provider" in source
    assert "validate_startup_config" in source


def test_module_cli_subprocess_invalid():
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "groq",
        "PYTHONPATH": str(_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "sre_agent.provider_config"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "startup config invalid" in proc.stderr.lower()
