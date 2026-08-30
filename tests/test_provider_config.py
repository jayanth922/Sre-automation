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
    for provider in ("groq", "anthropic", "openai_compatible"):
        assert pc.require_supported_provider(provider) == provider


@pytest.mark.parametrize(
    "bad",
    ["ollama", "nvidia", "gemini", "openai", "bogus", ""],
)
def test_unsupported_providers_fail_closed(bad):
    with pytest.raises(pc.ProviderConfigError) as exc:
        pc.require_supported_provider(bad)
    msg = str(exc.value).lower()
    assert "not supported" in msg or "unset" in msg or "unsupported" in msg
    # Never suggests silently using groq as a coercion path for aliases.
    if bad == "ollama":
        assert "openai_compatible" in msg
        assert "llm_base_url" in msg


def test_groq_requires_real_key():
    with pytest.raises(pc.ProviderConfigError, match="GROQ_API_KEY"):
        pc.validate_provider_credentials("groq", {"GROQ_API_KEY": "YOUR_KEY"})
    with pytest.raises(pc.ProviderConfigError, match="GROQ_API_KEY"):
        pc.validate_provider_credentials("groq", {"GROQ_API_KEY": ""})
    pc.validate_provider_credentials("groq", {"GROQ_API_KEY": "gsk_live_test_value"})


def test_openai_compatible_requires_base_and_model():
    with pytest.raises(pc.ProviderConfigError, match="LLM_BASE_URL"):
        pc.validate_provider_credentials(
            "openai_compatible",
            {"LLM_BASE_URL": "", "LLM_MODEL": "llama3"},
        )
    with pytest.raises(pc.ProviderConfigError, match="LLM_MODEL"):
        pc.validate_provider_credentials(
            "openai_compatible",
            {"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": ""},
        )
    pc.validate_provider_credentials(
        "openai_compatible",
        {
            "LLM_BASE_URL": "http://localhost:11434/v1",
            "LLM_MODEL": "llama3.1",
            "LLM_API_KEY": "not-needed",
        },
    )


def test_validate_startup_config_happy_path():
    env = {
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "groq",
        "GROQ_API_KEY": "gsk_test_key",
    }
    assert pc.validate_startup_config(env) == "groq"


def test_validate_startup_rejects_ollama_example():
    env = {
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "ollama",
        "GROQ_API_KEY": "gsk_test_key",
    }
    with pytest.raises(pc.ProviderConfigError, match="openai_compatible"):
        pc.validate_startup_config(env)


def test_cli_exits_nonzero_on_invalid(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert pc.main([]) == 1


def test_cli_exits_zero_on_valid(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key")
    assert pc.main([]) == 0


def test_runtime_no_longer_coerces_provider():
    """Source guard: agent_runtime must not silently default invalid providers to groq."""
    source = (_ROOT / "sre_agent" / "agent_runtime.py").read_text(encoding="utf-8")
    assert "defaulting to 'groq'" not in source
    assert "require_supported_provider" in source
    assert "validate_startup_config" in source


def test_module_cli_subprocess_invalid():
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "SECRET_KEY": "dev-secret",
        "LLM_PROVIDER": "ollama",
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
