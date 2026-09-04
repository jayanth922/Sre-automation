#!/usr/bin/env python3
"""
LiteLLM backend for the model router (competitive-audit upgrade #3).

Our router decides the *tier* (fast/balanced/strong) by task; LiteLLM is the
industry-standard layer that actually talks to 100+ providers and does the
cost/budget/fallback plumbing. When enabled, the router builds its LLM via
LiteLLM (through LangChain's ``ChatLiteLLM``, so ``.with_structured_output`` /
``.ainvoke`` still work), keeping our SRE tier policy on top of LiteLLM's routing.

Enabled with ``MODEL_ROUTER_BACKEND=litellm``. Per-tier model via
``MODEL_ROUTER_<TIER>_LITELLM_MODEL`` (LiteLLM model strings, e.g.
``groq/llama-3.3-70b-versatile``, ``gpt-4o``, ``anthropic/claude-...``); falls
back to ``MODEL_ROUTER_<TIER>_MODEL``, then — since Sentinel resolves its LLM
provider/model per cluster (``LLM_PROVIDER``, dashboard Settings), not from a
single global config — to a LiteLLM model string *derived* from whatever
provider/model the router already resolved for this call. That derivation is
what makes ``MODEL_ROUTER_BACKEND=litellm`` a safe platform-wide default: it
never assumes Anthropic when a tenant's cluster is actually configured for
Gemini. If no model can be resolved at all, the router uses its normal
provider path — so this is purely additive.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# LiteLLM's provider-routing prefix for each provider Sentinel supports
# (provider_config.SUPPORTED_PROVIDERS). Anything outside this map has no
# derived default — only an explicit MODEL_ROUTER_*_LITELLM_MODEL works.
_LITELLM_PREFIX = {"anthropic": "anthropic", "gemini": "gemini"}


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


def build_litellm_llm(model: str, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Any:
    """Build a LangChain-compatible LLM backed by LiteLLM. Guarded import."""
    try:
        import litellm  # noqa: F401 - validates the optional runtime dependency
        from langchain_litellm import ChatLiteLLM  # maintained wrapper; lazy import
    except Exception as e:
        raise RuntimeError(
            "LiteLLM backend requested but ChatLiteLLM unavailable. Install with: "
            "pip install litellm langchain-litellm"
        ) from e

    kwargs: dict = {"model": model}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    logger.info(f"ModelRouter: using LiteLLM backend (model={model})")
    return ChatLiteLLM(**kwargs)
