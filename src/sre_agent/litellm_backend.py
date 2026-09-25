#!/usr/bin/env python3
"""
LiteLLM backend for the model router (competitive-audit upgrade #3).

Our router decides the *tier* (fast/balanced/strong) by task; LiteLLM is the
provider-compatible transport layer. When enabled, the router builds its LLM via
LiteLLM (through LangChain's ``ChatLiteLLM``, so ``.with_structured_output`` /
``.ainvoke`` still work), keeping our SRE tier policy and one explicit model on
top. This adapter does not configure a hidden fallback model.

Enabled with ``MODEL_ROUTER_BACKEND=litellm``. Per-tier model via
``MODEL_ROUTER_<TIER>_LITELLM_MODEL`` (LiteLLM model strings, e.g.
``groq/llama-3.3-70b-versatile``, ``gpt-4o``, ``anthropic/claude-...``); falls
back to ``MODEL_ROUTER_<TIER>_MODEL``, then — since Sentinel resolves its LLM
provider/model per cluster (``LLM_PROVIDER``, dashboard Settings), not from a
single global config — to a LiteLLM model string *derived* from whatever
provider/model the router already resolved for this call. If no model can be
resolved at all, the router uses its normal provider path — so this is purely
additive.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# LiteLLM's provider-routing prefix for each provider Sentinel supports
# (provider_config.SUPPORTED_PROVIDERS). Anything outside this map has no
# derived default — only an explicit MODEL_ROUTER_*_LITELLM_MODEL works.
_LITELLM_PREFIX = {"anthropic": "anthropic"}


def litellm_enabled() -> bool:
    return os.getenv("MODEL_ROUTER_BACKEND", "").lower() == "litellm" or \
        os.getenv("LITELLM_ENABLED", "").lower() in ("true", "1", "yes")


def default_litellm_model(provider: str, model_id: Optional[str]) -> Optional[str]:
    """Derive a LiteLLM model string from an already-resolved provider/model.

    ``model_id`` may be ``None`` (the router's "use the provider's default"
    convention) — resolved here via the same ``SREConstants.get_model_config``
    every other call path uses, so this never drifts from the real default.
    """
    prefix = _LITELLM_PREFIX.get(provider)
    if not prefix:
        return None
    if not model_id:
        try:
            from .constants import SREConstants
        except ImportError:  # direct-file unit-test loading has no package context
            from sre_agent.constants import SREConstants

        model_id = SREConstants.get_model_config(provider).get("model_id")
    if not model_id:
        return None
    return model_id if model_id.startswith(f"{prefix}/") else f"{prefix}/{model_id}"


def tier_litellm_model(
    tier_value: str, *, provider: Optional[str] = None, model_id: Optional[str] = None
) -> Optional[str]:
    """LiteLLM model string for a tier (None → use the normal provider path).

    Resolution order: explicit ``MODEL_ROUTER_<TIER>_LITELLM_MODEL`` env, then
    the generic ``MODEL_ROUTER_<TIER>_MODEL`` env (back-compat), then — only
    when the caller passes the router's resolved ``provider``/``model_id`` —
    a derived LiteLLM string for that provider.
    """
    up = tier_value.upper()
    explicit = os.getenv(f"MODEL_ROUTER_{up}_LITELLM_MODEL") or os.getenv(f"MODEL_ROUTER_{up}_MODEL")
    if explicit:
        return explicit
    if provider:
        return default_litellm_model(provider, model_id)
    return None


# Models that reject any temperature other than a fixed value (e.g. Anthropic's
# extended-thinking-only models, which require temperature=1). Keyed by the
# model's bare name (no provider prefix) so it matches regardless of how the
# caller qualified it (``claude-opus-5`` or ``anthropic/claude-opus-5``).
_FIXED_TEMPERATURE: dict[str, float] = {
    "claude-opus-5": 1.0,
    "claude-sonnet-5": 1.0,
}


def _resolve_temperature(model: str, temperature: Optional[float]) -> Optional[float]:
    bare_model = model.rsplit("/", 1)[-1]
    fixed = _FIXED_TEMPERATURE.get(bare_model)
    if fixed is not None and temperature != fixed:
        logger.info(
            f"ModelRouter: {bare_model} only supports temperature={fixed}; "
            f"overriding requested temperature={temperature}"
        )
        return fixed
    return temperature


def build_litellm_llm(
    model: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Build a LangChain-compatible LLM backed by LiteLLM. Guarded import.

    ``api_key`` is the caller's already-decrypted per-cluster credential.
    Without it, LiteLLM/the provider SDK falls back to whatever is in the
    process's own ``ANTHROPIC_API_KEY`` env var — which on this platform is
    just a startup-validation placeholder, not a real usable key — so every
    LiteLLM-backed call must pass this explicitly rather than relying on env.
    """
    try:
        import litellm  # noqa: F401 - validates the optional runtime dependency
        from langchain_litellm import ChatLiteLLM  # maintained wrapper; lazy import
    except Exception as e:
        raise RuntimeError(
            "LiteLLM backend requested but ChatLiteLLM unavailable. Install with: "
            "pip install litellm langchain-litellm"
        ) from e

    # Lazy like the rest of this function's imports: the module stays loadable
    # standalone (tests/test_litellm_backend.py execs it by path), so it keeps
    # no package-relative imports at module scope.
    from .llm_retry import llm_max_retries

    temperature = _resolve_temperature(model, temperature)
    kwargs: dict = {"model": model}
    # ChatLiteLLM exposes no retry field of its own, so the setting travels via
    # ``model_kwargs``, which its ``_default_params`` spreads straight into the
    # underlying ``litellm.acompletion`` call. LiteLLM's client wrapper
    # implements ``num_retries`` with an exponential backoff strategy, and its
    # ``_should_retry`` already treats 429/500/503/529 as retryable — the 529
    # that killed three investigations on 2026-09-19 was retryable all along
    # and simply never had a budget. See src/sre_agent/llm_retry.py.
    kwargs["model_kwargs"] = {"num_retries": llm_max_retries()}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if api_key:
        kwargs["api_key"] = api_key
    logger.info(f"ModelRouter: using LiteLLM backend (model={model})")
    return ChatLiteLLM(**kwargs)
