#!/usr/bin/env python3
"""Fail-closed LLM provider and startup configuration validation.

Sentinel supports only Anthropic and Gemini. Invalid provider values must never
be silently replaced with a default. This module is deliberately stdlib-only so
deployment entrypoints can validate configuration before migrations or serving.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional

SUPPORTED_PROVIDERS = ("anthropic", "gemini")
DEFAULT_PROVIDER = "anthropic"

_ALIAS_HINTS: Mapping[str, str] = {
    "groq": "Groq support was removed; use LLM_PROVIDER=anthropic or gemini.",
    "ollama": "Ollama support was removed; use LLM_PROVIDER=anthropic or gemini.",
    "nvidia": "NVIDIA NIM support was removed; use LLM_PROVIDER=anthropic or gemini.",
    "openai": "OpenAI support was removed; use LLM_PROVIDER=anthropic or gemini.",
    "openai_compatible": (
        "OpenAI-compatible providers were removed; use LLM_PROVIDER=anthropic "
        "or gemini."
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
    return (
        lowered in _PLACEHOLDER_VALUES
        or lowered.startswith("your_")
        or lowered.startswith("your-")
        or "change_in_production" in lowered
        or "change-me" in lowered
    )


def require_supported_provider(raw: Optional[str]) -> str:
    """Return a normalized supported provider or raise ``ProviderConfigError``."""
    provider = (raw or "").strip().lower()
    if not provider:
        raise ProviderConfigError(
            "LLM_PROVIDER is unset. Set it to one of: "
            + ", ".join(SUPPORTED_PROVIDERS)
            + "."
        )
    if provider in SUPPORTED_PROVIDERS:
        return provider
    if provider in _ALIAS_HINTS:
        raise ProviderConfigError(_ALIAS_HINTS[provider])
    raise ProviderConfigError(
        f"Unsupported LLM_PROVIDER={provider!r}. Supported: "
        + ", ".join(SUPPORTED_PROVIDERS)
        + "."
    )


def validate_provider_credentials(
    provider: str,
    environ: Optional[Mapping[str, str]] = None,
    *,
    api_key: Optional[str] = None,
) -> None:
    """Ensure the selected provider has the credentials/settings it needs."""
    provider = require_supported_provider(provider)

    if provider == "anthropic":
        key = (api_key or _env(environ, "ANTHROPIC_API_KEY")).strip()
        if _is_placeholder(key):
            raise ProviderConfigError(
                "LLM_PROVIDER=anthropic requires a real ANTHROPIC_API_KEY."
            )
        return

    if provider == "gemini":
        key = (
            api_key
            or _env(environ, "GOOGLE_API_KEY")
            or _env(environ, "GEMINI_API_KEY")
        ).strip()
        if _is_placeholder(key):
            raise ProviderConfigError(
                "LLM_PROVIDER=gemini requires a real GOOGLE_API_KEY or GEMINI_API_KEY."
            )
        return


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
    if not secret or _is_placeholder(secret):
        raise ProviderConfigError(
            "SECRET_KEY is unset or still a placeholder. Set a stable secret "
            "before starting the API (see .env.example)."
        )

    if not require_llm:
        return ""

    if _env(source, "SKIP_LLM_VALIDATION").lower() in {"1", "true", "yes"}:
        return _env(source, "LLM_PROVIDER", DEFAULT_PROVIDER).lower()

    # A bad/legacy provider *name* is a real misconfiguration (typo, or a
    # provider that was removed) — that still fails closed so it's never
    # silently swapped for a default. A missing/placeholder *credential* is
    # not a misconfiguration, just "not set up yet": clients configure their
    # real LLM key per cluster from the dashboard (Settings) and it's
    # enforced strictly there (see agent_runtime._build_runtime), so boot
    # only warns here instead of refusing to start the whole platform over
    # a key that may simply not be filled in yet.
    provider = require_supported_provider(
        _env(source, "LLM_PROVIDER") or DEFAULT_PROVIDER
    )
    try:
        validate_provider_credentials(provider, source)
    except ProviderConfigError as exc:
        print(
            f"startup config warning: {exc} LLM-dependent features (incident "
            "investigation) won't work until a real key is set — add it to "
            "the platform env or per-cluster in the dashboard's Settings.",
            file=sys.stderr,
        )
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
