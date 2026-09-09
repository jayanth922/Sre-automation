"""Resolve a cluster's effective runtime context — the single source of truth
for per-cluster overrides.

Only auth/org is platform-wide; everything a client brings (LLM brain, scope,
integration credentials) resolves per-cluster here. A bound cluster that
hasn't set an override gets ``None``, not a silently-substituted platform
default — ``authorize_llm``/``resolve_authorized_llm`` then fail closed with a
clear error. The ``LLM_PROVIDER``/``LLM_MODEL``/``LLM_BASE_URL``/``LLM_API_KEY``
env vars only apply when there is no cluster at all (local development /
self-hosted single-tenant mode, i.e. ``cluster is None``).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

SUPPORTED_LLM_PROVIDERS = frozenset({"anthropic", "gemini"})


class UnauthorizedLLMConfigError(ValueError):
    """Raised when a cluster LLM setting is outside the operator allowlist."""


def _attr(cluster: Any, name: str) -> Optional[str]:
    val = getattr(cluster, name, None) if cluster is not None else None
    return val if (val is not None and str(val).strip() != "") else None


def allowed_llm_providers() -> frozenset[str]:
    """Operator allowlist; empty env keeps the built-in supported set."""
    raw = os.getenv("ALLOWED_LLM_PROVIDERS", "").strip()
    if not raw:
        return SUPPORTED_LLM_PROVIDERS
    allowed = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    return allowed or SUPPORTED_LLM_PROVIDERS


def allowed_llm_models() -> Optional[frozenset[str]]:
    """Operator model allowlist; empty env means no model restriction."""
    raw = os.getenv("ALLOWED_LLM_MODELS", "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def llm_run_budget() -> Optional[float]:
    """Optional remaining USD/credits budget for LLM construction.

    When set to ``0`` or negative, authorization fails closed. Unset means
    unmetered at the cluster-resolution boundary (per-call budgets still apply
    via the model router ``RequestContext``).
    """
    raw = os.getenv("LLM_RUN_BUDGET", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise UnauthorizedLLMConfigError(
            f"LLM_RUN_BUDGET must be numeric, got {raw!r}"
        ) from exc


def resolve_llm(cluster: Any) -> Dict[str, Optional[str]]:
    """Per-cluster LLM brain.

    Every field prefers the cluster override; all four only fall back to the
    platform env when there is no cluster bound at all (``cluster is None`` —
    local development / self-hosted single-tenant mode). A real cluster that
    hasn't set ``llm_provider`` gets ``provider=None`` here, not a silently
    substituted default — callers must fail closed on that instead.
    """
    use_env_fallbacks = cluster is None
    provider = _attr(cluster, "llm_provider") or (
        os.getenv("LLM_PROVIDER", "anthropic") if use_env_fallbacks else None
    )
    return {
        "provider": provider.strip().lower() if provider else None,
        "model": _attr(cluster, "llm_model")
        or (os.getenv("LLM_MODEL") if use_env_fallbacks else None),
        "base_url": _attr(cluster, "llm_base_url")
        or (os.getenv("LLM_BASE_URL") if use_env_fallbacks else None),
        "api_key": _attr(cluster, "llm_api_key")
        or (os.getenv("LLM_API_KEY") if use_env_fallbacks else None),
    }


def authorize_llm(
    provider: Optional[str],
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Fail closed when provider/model/budget are outside operator policy."""
    resolved_provider = (provider or "").strip().lower()
    if not resolved_provider:
        raise UnauthorizedLLMConfigError(
            "No LLM provider configured for this cluster. Set one explicitly "
            "in cluster Settings (or LLM_PROVIDER for local/self-hosted mode "
            "with no bound cluster) — there is no silent platform default."
        )
    if resolved_provider not in SUPPORTED_LLM_PROVIDERS:
        raise UnauthorizedLLMConfigError(
            f"Unsupported LLM provider {resolved_provider!r}; "
            f"supported: {sorted(SUPPORTED_LLM_PROVIDERS)}"
        )
    allowed_providers = allowed_llm_providers()
    if resolved_provider not in allowed_providers:
        raise UnauthorizedLLMConfigError(
            f"LLM provider {resolved_provider!r} is not in ALLOWED_LLM_PROVIDERS"
        )

    resolved_model = model.strip() if model and str(model).strip() else None
    model_allowlist = allowed_llm_models()
    if (
        resolved_model
        and model_allowlist is not None
        and resolved_model not in model_allowlist
    ):
        raise UnauthorizedLLMConfigError(
            f"LLM model {resolved_model!r} is not in ALLOWED_LLM_MODELS"
        )

    budget = llm_run_budget()
    if budget is not None and budget <= 0:
        raise UnauthorizedLLMConfigError(
            "LLM_RUN_BUDGET exhausted; refusing LLM configuration"
        )

    return {
        "provider": resolved_provider,
        "model": resolved_model,
        "base_url": base_url.strip() if base_url and str(base_url).strip() else None,
        "api_key": api_key.strip() if api_key and str(api_key).strip() else None,
    }


def resolve_authorized_llm(cluster: Any) -> Dict[str, Optional[str]]:
    """Resolve then authorize the effective LLM brain for a cluster."""
    resolved = resolve_llm(cluster)
    return authorize_llm(
        resolved["provider"],
        model=resolved["model"],
        base_url=resolved["base_url"],
        api_key=resolved["api_key"],
    )


def llm_manifest(values: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """Trace/UI-facing LLM fields with no secrets."""
    provider = values.get("provider") or values.get("llm_provider")
    model = values.get("model") or values.get("llm_model") or values.get("model_id")
    base_url = values.get("base_url") or values.get("llm_base_url")
    return {
        "provider": str(provider).strip().lower() if provider else None,
        "model": str(model).strip() if model else None,
        "base_url": str(base_url).strip() if base_url else None,
    }


def resolve_namespace(cluster: Any) -> Optional[str]:
    """The cluster's namespace scope, or None for whole-cluster (infra) scope."""
    return _attr(cluster, "namespace")
