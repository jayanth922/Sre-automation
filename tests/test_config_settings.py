#!/usr/bin/env python3
"""Tests for typed settings and strict boolean parsing (P02)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    name = "sre_agent_config_under_test"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "sre_agent" / "config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # pydantic must resolve `sre_agent.config` style only if imported as package;
    # loading by path is fine for this module (no relative imports).
    spec.loader.exec_module(module)
    return module


cfg = _load()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        (None, False),
        (True, True),
        (False, False),
    ],
)
def test_parse_bool_accepted(raw, expected):
    assert cfg.parse_bool(raw, default=False, name="DEBUG") is expected


@pytest.mark.parametrize("raw", ["maybe", "debug", "enabled", "2", "falsey"])
def test_parse_bool_rejects_ambiguous(raw):
    with pytest.raises(cfg.SettingsError, match="Invalid boolean"):
        cfg.parse_bool(raw, name="DEBUG")


def test_debug_false_string_is_false():
    """Regression: string truthiness treated DEBUG=false as enabled."""
    assert bool("false") is True  # why the bug existed
    env = {
        "SECRET_KEY": "test-secret",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    settings = cfg.load_settings(env)
    assert settings.debug is False


def test_debug_true_variants():
    env = {
        "SECRET_KEY": "test-secret",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    assert cfg.load_settings(env).debug is True


def test_secrets_redacted_in_repr():
    env = {
        "SECRET_KEY": "super-secret-key",
        "GROQ_API_KEY": "gsk_live_secret",
        "MCP_SERVICE_TOKEN": "mcp-secret",
        "DATABASE_URL": "postgresql+asyncpg://dbuser:dbpass@dbhost:5432/sre",
        "REDIS_URL": "redis://:redispass@redis:6379/0",
        "DEBUG": "false",
    }
    settings = cfg.load_settings(env)
    text = repr(settings)
    assert "super-secret-key" not in text
    assert "gsk_live_secret" not in text
    assert "mcp-secret" not in text
    assert "dbpass" not in text
    assert "redispass" not in text
    assert "**********" in text
    assert settings.secret_key.get_secret_value() == "super-secret-key"


def test_invalid_live_bus_backend():
    with pytest.raises(cfg.SettingsError, match="LIVE_BUS_BACKEND"):
        cfg.load_settings(
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
                "REDIS_URL": "redis://localhost:6379/0",
                "LIVE_BUS_BACKEND": "kafka",
            }
        )


def test_production_defaults_cookie_secure():
    env = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    settings = cfg.load_settings(env)
    assert settings.cookie_secure is True
    assert settings.debug is False


def test_production_cookie_secure_can_be_disabled():
    env = {
        "APP_ENV": "production",
        "COOKIE_SECURE": "false",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    assert cfg.load_settings(env).cookie_secure is False


def test_database_module_uses_strict_debug(monkeypatch):
    """Import database with DEBUG=false and confirm engine.echo is off."""
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db"
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    # Ensure a clean settings cache + module import.
    sys.modules.pop("sre_agent.config", None)
    sys.modules.pop("backend.database", None)
    # Load real package modules.
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    import sre_agent.config as real_cfg

    real_cfg.clear_settings_cache()
    import backend.database as db

    assert db.engine.echo is False
    source = (_ROOT / "backend" / "database.py").read_text(encoding="utf-8")
    assert "os.getenv(\"DEBUG\")" not in source or "echo=True if os.getenv" not in source
    assert "echo=_settings.debug" in source
