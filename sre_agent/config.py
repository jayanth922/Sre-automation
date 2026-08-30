#!/usr/bin/env python3
"""Typed process settings (P02).

Environment flags must not use Python string truthiness. ``DEBUG=false`` is a
non-empty string, so ``if os.getenv("DEBUG")`` wrongly enables SQL echo and other
debug paths. This module is the single parser for booleans, common enums, URLs,
and secret values, with redacted representations for logs.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from functools import lru_cache
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_TRUE = frozenset({"1", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "no", "n", "off", ""})


class SettingsError(ValueError):
    """Invalid environment configuration."""


class AppEnvironment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class AgentMode(str, Enum):
    API = "api"
    STANDALONE = "standalone"


class LiveBusBackend(str, Enum):
    MEMORY = "memory"
    REDIS = "redis"


class CheckpointerBackend(str, Enum):
    MEMORY = "memory"
    REDIS = "redis"
    POSTGRES = "postgres"


def parse_bool(value: Any, *, default: bool = False, name: str = "flag") -> bool:
    """Strict boolean parse. Rejects ambiguous values instead of coercing."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SettingsError(
        f"Invalid boolean for {name}={value!r}. "
        "Use true/false, 1/0, yes/no, or on/off."
    )


def _enum_value(enum_cls: type[Enum], raw: str, *, name: str) -> Enum:
    text = (raw or "").strip().lower()
    for member in enum_cls:
        if member.value == text:
            return member
    allowed = ", ".join(m.value for m in enum_cls)
    raise SettingsError(f"Invalid {name}={raw!r}. Allowed: {allowed}.")


def _optional_secret(raw: Optional[str]) -> Optional[SecretStr]:
    if raw is None:
        return None
    text = str(raw)
    if not text:
        return None
    return SecretStr(text)


def _validate_url(raw: str, *, name: str, allowed_schemes: set[str]) -> str:
    text = (raw or "").strip()
    if not text:
        raise SettingsError(f"{name} must not be empty")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise SettingsError(
            f"Invalid {name} scheme {scheme!r}. Expected one of: "
            + ", ".join(sorted(allowed_schemes))
        )
    if not parsed.netloc and scheme not in {"memory"}:
        # postgresql+asyncpg://user:pass@host/db — netloc present
        # also allow redis://localhost:6379/0
        raise SettingsError(f"Invalid {name}: missing host in {text!r}")
    return text


def _clean_port(value: str) -> str:
    """Recover a numeric port when Kubernetes injects tcp://host:port."""
    match = re.search(r"(\d+)\s*$", value or "")
    return match.group(1) if match else "5432"


class Settings(BaseModel):
    """Process-wide configuration loaded from the environment."""

    model_config = ConfigDict(
        extra="ignore",
        validate_assignment=True,
        # Keep SecretStr opaque in the default repr.
        str_strip_whitespace=True,
    )

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    agent_mode: AgentMode = AgentMode.API
    debug: bool = False

    database_url: str
    sync_database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    secret_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    google_api_key: Optional[SecretStr] = None
    anthropic_api_key: Optional[SecretStr] = None
    groq_api_key: Optional[SecretStr] = None
    llm_api_key: Optional[SecretStr] = None
    mcp_service_token: Optional[SecretStr] = None

    llm_provider: str = "anthropic"
    live_bus_backend: LiveBusBackend = LiveBusBackend.MEMORY
    checkpointer_enabled: bool = False
    checkpointer_backend: CheckpointerBackend = CheckpointerBackend.MEMORY
    act_phase_enabled: bool = False
    cookie_secure: bool = False

    @model_validator(mode="after")
    def _derive_sync_url_and_env_defaults(self) -> "Settings":
        if not self.sync_database_url:
            self.sync_database_url = self.database_url.replace(
                "postgresql+asyncpg", "postgresql"
            )
        # Production defaults: never leave cookie_secure accidentally false when
        # APP_ENV=production unless the operator set COOKIE_SECURE explicitly
        # (handled at load time). Debug stays false unless explicitly enabled.
        if self.app_env == AppEnvironment.PRODUCTION and self.debug:
            # Explicit DEBUG=true in production is allowed but unusual; keep it.
            pass
        return self

    def __repr__(self) -> str:
        return (
            "Settings("
            f"app_env={self.app_env.value!r}, "
            f"agent_mode={self.agent_mode.value!r}, "
            f"debug={self.debug!r}, "
            f"database_url='{_redact_url(self.database_url)}', "
            f"redis_url='{_redact_url(self.redis_url)}', "
            f"secret_key={_secret_repr(self.secret_key)}, "
            f"groq_api_key={_secret_repr(self.groq_api_key)}, "
            f"anthropic_api_key={_secret_repr(self.anthropic_api_key)}, "
            f"llm_api_key={_secret_repr(self.llm_api_key)}, "
            f"mcp_service_token={_secret_repr(self.mcp_service_token)}, "
            f"llm_provider={self.llm_provider!r}, "
            f"live_bus_backend={self.live_bus_backend.value!r}, "
            f"checkpointer_enabled={self.checkpointer_enabled!r}, "
            f"checkpointer_backend={self.checkpointer_backend.value!r}, "
            f"act_phase_enabled={self.act_phase_enabled!r}, "
            f"cookie_secure={self.cookie_secure!r}"
            ")"
        )

    __str__ = __repr__


def _secret_repr(value: Optional[SecretStr]) -> str:
    if value is None or not value.get_secret_value():
        return "SecretStr('')"
    return "SecretStr('**********')"


def _redact_url(url: str) -> str:
    """Hide userinfo (passwords) in database/redis URLs for logs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "***"
    if not parsed.password and "@" not in (parsed.netloc or ""):
        return url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = parsed.username or ""
    auth = f"{user}:**********@" if user or parsed.password else ""
    return f"{parsed.scheme}://{auth}{host}{port}{parsed.path}"


def _build_database_url(environ: Mapping[str, str]) -> str:
    explicit = (environ.get("DATABASE_URL") or "").strip()
    if explicit:
        return _validate_url(
            explicit,
            name="DATABASE_URL",
            allowed_schemes={"postgresql", "postgresql+asyncpg"},
        )
    user = environ.get("POSTGRES_USER", "sre_user")
    password = environ.get("POSTGRES_PASSWORD", "sre_password")
    host = environ.get("POSTGRES_HOST", "postgres")
    port = _clean_port(environ.get("POSTGRES_PORT", "5432"))
    db = environ.get("POSTGRES_DB", "sre_platform")
    return (
        f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    )


def load_settings(environ: Optional[Mapping[str, str]] = None) -> Settings:
    """Load and validate settings from ``environ`` (default: ``os.environ``)."""
    env: Mapping[str, str] = environ if environ is not None else os.environ

    app_env_raw = (
        env.get("APP_ENV")
        or env.get("SENTINEL_ENV")
        or env.get("ENVIRONMENT")
        or "development"
    )
    app_env = _enum_value(AppEnvironment, app_env_raw, name="APP_ENV")

    # Environment-specific defaults (explicit env always wins).
    default_cookie_secure = app_env == AppEnvironment.PRODUCTION

    debug = parse_bool(env.get("DEBUG"), default=False, name="DEBUG")
    cookie_secure = parse_bool(
        env.get("COOKIE_SECURE"),
        default=default_cookie_secure,
        name="COOKIE_SECURE",
    )

    database_url = _build_database_url(env)
    redis_raw = (env.get("REDIS_URL") or "redis://localhost:6379/0").strip()
    redis_url = _validate_url(
        redis_raw, name="REDIS_URL", allowed_schemes={"redis", "rediss"}
    )

    # Provider allow-list is enforced by provider_config (P01); here we only
    # normalize the string so settings stay loadable with legacy env files.
    llm_provider = (env.get("LLM_PROVIDER") or "anthropic").strip().lower() or "anthropic"

    live_bus = _enum_value(
        LiveBusBackend,
        (env.get("LIVE_BUS_BACKEND") or LiveBusBackend.MEMORY.value),
        name="LIVE_BUS_BACKEND",
    )
    checkpointer_backend = _enum_value(
        CheckpointerBackend,
        (env.get("CHECKPOINTER_BACKEND") or CheckpointerBackend.MEMORY.value),
        name="CHECKPOINTER_BACKEND",
    )
    agent_mode = _enum_value(
        AgentMode,
        (env.get("AGENT_MODE") or AgentMode.API.value),
        name="AGENT_MODE",
    )

    return Settings(
        app_env=app_env,
        agent_mode=agent_mode,
        debug=debug,
        database_url=database_url,
        redis_url=redis_url,
        secret_key=_optional_secret(env.get("SECRET_KEY")) or SecretStr(""),
        groq_api_key=_optional_secret(env.get("GROQ_API_KEY")),
        anthropic_api_key=_optional_secret(env.get("ANTHROPIC_API_KEY")),
        llm_api_key=_optional_secret(env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")),
        mcp_service_token=_optional_secret(env.get("MCP_SERVICE_TOKEN")),
        llm_provider=llm_provider,
        live_bus_backend=live_bus,
        checkpointer_enabled=parse_bool(
            env.get("CHECKPOINTER_ENABLED"),
            default=False,
            name="CHECKPOINTER_ENABLED",
        ),
        checkpointer_backend=checkpointer_backend,
        act_phase_enabled=parse_bool(
            env.get("ACT_PHASE_ENABLED"), default=False, name="ACT_PHASE_ENABLED"
        ),
        cookie_secure=cookie_secure,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for the running process."""
    return load_settings()


def clear_settings_cache() -> None:
    """Drop the cached settings (tests / after env mutation)."""
    get_settings.cache_clear()
