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
back to ``MODEL_ROUTER_<TIER>_MODEL``. If no model is configured for a tier, the
router uses its normal provider path — so this is purely additive.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def litellm_enabled() -> bool:
    return os.getenv("MODEL_ROUTER_BACKEND", "").lower() == "litellm" or \
        os.getenv("LITELLM_ENABLED", "").lower() in ("true", "1", "yes")


def tier_litellm_model(tier_value: str) -> Optional[str]:
    """LiteLLM model string for a tier, from env (None → use the normal path)."""
    up = tier_value.upper()
    return os.getenv(f"MODEL_ROUTER_{up}_LITELLM_MODEL") or os.getenv(f"MODEL_ROUTER_{up}_MODEL")


def build_litellm_llm(model: str, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Any:
    """Build a LangChain-compatible LLM backed by LiteLLM. Guarded import."""
    try:
        import litellm  # noqa: F401 - validates the optional runtime dependency
        from langchain_community.chat_models import ChatLiteLLM  # lazy; optional dep
    except Exception as e:
        raise RuntimeError(
            "LiteLLM backend requested but ChatLiteLLM unavailable. Install with: "
            "pip install litellm langchain-community"
        ) from e

    kwargs: dict = {"model": model}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    logger.info(f"ModelRouter: using LiteLLM backend (model={model})")
    return ChatLiteLLM(**kwargs)
