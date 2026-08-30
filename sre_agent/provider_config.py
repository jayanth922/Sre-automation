#!/usr/bin/env python3
"""LLM provider configuration validation (P01).

The runtime accepts only structured-output-capable providers:
``groq``, ``anthropic``, and ``openai_compatible``. Unsupported values must
never be silently coerced to Groq — startup fails with an actionable error.

This module is intentionally stdlib-only so ``platform/start.sh`` and the API
container entrypoint can validate env before Alembic migrations or uvicorn.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional

SUPPORTED_PROVIDERS = ("groq", "anthropic", "openai_compatible")

# Historical / docs aliases → how to migrate (never auto-selected).
_ALIAS_HINTS: Mapping[str, str] = {
    "ollama": (
        "LLM_PROVIDER=ollama is not supported. Use "
        "LLM_PROVIDER=openai_compatible with "
        "LLM_BASE_URL pointing at Ollama's OpenAI-compatible endpoint "
        "(e.g. http://host.docker.internal:11434/v1) and LLM_MODEL=<model>."
    ),
    "nvidia": (
        "LLM_PROVIDER=nvidia is not supported. Use "
        "LLM_PROVIDER=openai_compatible with "
        "LLM_BASE_URL=https://integrate.api.nvidia.com/v1 (or your NIM URL), "
        "LLM_API_KEY=$NVIDIA_API_KEY, and LLM_MODEL=<nvidia model id>."
    ),
    "gemini": (
        "LLM_PROVIDER=gemini is not supported. Use "
        "LLM_PROVIDER=openai_compatible against a Gemini OpenAI-compatible "
        "gateway, or switch to LLM_PROVIDER=groq / anthropic."
    ),
    "openai": (
        "LLM_PROVIDER=openai is not supported. Use "
        "LLM_PROVIDER=openai_compatible with LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
        "(OpenAI's API is OpenAI-compatible)."
    ),
}

_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "your_key",
        "your-key",
        "changeme",
        "change_me",
        "todo",
        "replace_me",
        "your_nvidia_api_key",
        "your_api_key",
    }
)


class ProviderConfigError(ValueError):
    """Invalid or incomplete LLM provider configuration."""


def _env(environ: Optional[Mapping[str, str]], key: str, default: str = "") -> str:
    source: Mapping[str, str] = environ if environ is not None else os.environ
    raw = source.get(key, default)
    return (raw or "").strip()


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if lowered in _PLACEHOLDER_VALUES:
        return True
    if lowered.startswith("your_") or lowered.startswith("your-"):
        return True
    if "change_in_production" in lowered or "change-me" in lowered:
        return True
    return False


def require_supported_provider(raw: Optional[str]) -> str:
    """Return a normalized supported provider or raise ``ProviderConfigError``."""
    provider = (raw or "").strip().lower()
    if not provider:
        raise ProviderConfigError(
            "LLM_PROVIDER is unset. Set it to one of: "
            + ", ".join(SUPPORTED_PROVIDERS)
            + ". Default documented setup uses groq."
        )
    if provider in SUPPORTED_PROVIDERS:
        return provider
    if provider in _ALIAS_HINTS:
        raise ProviderConfigError(_ALIAS_HINTS[provider])
    raise ProviderConfigError(
        f"Unsupported LLM_PROVIDER={provider!r}. Supported: "
        + ", ".join(SUPPORTED_PROVIDERS)
        + ". For a self-hosted model use openai_compatible + LLM_BASE_URL."
    )


def validate_provider_credentials(
    provider: str,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Ensure the selected provider has the credentials/settings it needs."""
    provider = require_supported_provider(provider)

    if provider == "groq":
        key = _env(environ, "GROQ_API_KEY")
        if _is_placeholder(key):
            raise ProviderConfigError(
                "LLM_PROVIDER=groq requires a real GROQ_API_KEY "
                "(not empty or a YOUR_KEY placeholder)."
            )
        return

    if provider == "anthropic":
        key = _env(environ, "ANTHROPIC_API_KEY")
        if _is_placeholder(key):
            raise ProviderConfigError(
                "LLM_PROVIDER=anthropic requires a real ANTHROPIC_API_KEY."
            )
        return

    # openai_compatible
    base_url = _env(environ, "LLM_BASE_URL") or _env(environ, "OPENAI_BASE_URL")
    model = _env(environ, "LLM_MODEL") or _env(environ, "OPENAI_MODEL")
    missing = []
    if _is_placeholder(base_url) or not base_url:
        missing.append("LLM_BASE_URL (e.g. http://host.docker.internal:11434/v1)")
    if _is_placeholder(model) or not model:
        missing.append("LLM_MODEL (the model id served at that base URL)")
    if missing:
        raise ProviderConfigError(
            "LLM_PROVIDER=openai_compatible is missing required settings: "
            + "; ".join(missing)
            + "."
        )


def validate_startup_config(
    environ: Optional[Mapping[str, str]] = None,
    *,
    require_llm: bool = True,
) -> str:
    """Validate process env before migrations / serving.

    Returns the normalized ``LLM_PROVIDER`` when LLM checks run.
    """
    source: Mapping[str, str] = environ if environ is not None else os.environ

    secret = _env(source, "SECRET_KEY")
    if not secret:
        raise ProviderConfigError(
            "SECRET_KEY is unset. Set a stable secret before starting the API "
            "(see .env.example)."
        )

    if not require_llm:
        return ""

    # Control-plane-only processes can skip LLM by setting SKIP_LLM_VALIDATION=1
    # (tests / tooling). Normal API/incident paths must not.
    if _env(source, "SKIP_LLM_VALIDATION").lower() in {"1", "true", "yes"}:
        return _env(source, "LLM_PROVIDER", "groq").lower()

    provider = require_supported_provider(_env(source, "LLM_PROVIDER") or "groq")
    validate_provider_credentials(provider, source)
    return provider


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: exit 0 when config is valid, 1 with a precise message otherwise."""
    args = list(sys.argv[1:] if argv is None else argv)
    require_llm = "--no-llm" not in args
    try:
        provider = validate_startup_config(require_llm=require_llm)
        if require_llm:
            print(f"startup config ok: LLM_PROVIDER={provider}")
        else:
            print("startup config ok (LLM checks skipped)")
        return 0
    except ProviderConfigError as exc:
        print(f"startup config invalid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
