#!/usr/bin/env python3
"""
Centralized LLM utilities with improved error handling.

This module provides a single point for LLM creation with proper error handling
for authentication, access, and configuration issues.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from .constants import SREConstants
from .provider_config import DEFAULT_PROVIDER, SUPPORTED_PROVIDERS, require_supported_provider

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """Exception raised when LLM provider creation fails."""

    pass


class LLMAuthenticationError(LLMProviderError):
    """Exception raised when LLM authentication fails."""

    pass


class LLMAccessError(LLMProviderError):
    """Exception raised when LLM access is denied."""

    pass


def create_llm_with_error_handling(provider: str = DEFAULT_PROVIDER, **kwargs):
    """Create LLM instance with proper error handling and helpful error messages.

    Args:
        provider: LLM provider — ``anthropic`` or ``gemini``
        **kwargs: Additional configuration overrides

    Returns:
        LLM instance

    Raises:
        LLMProviderError: For general provider errors
        LLMAuthenticationError: For authentication failures
        LLMAccessError: For access/permission failures
        ValueError: For unsupported providers
    """
    provider = require_supported_provider(provider)
    logger.info(f"Creating LLM with provider: {provider}")

    try:
        config = SREConstants.get_model_config(provider, **kwargs)

        if provider == "anthropic":
            logger.info(f"Creating Anthropic (Claude) LLM - Model: {config['model_id']}")
            return _create_anthropic_llm(config)
        elif provider == "gemini":
            logger.info(f"Creating Gemini LLM - Model: {config['model_id']}")
            return _create_gemini_llm(config)

    except Exception as e:
        error_msg = _get_helpful_error_message(provider, e)
        logger.error(f"Failed to create LLM: {error_msg}")

        # Classify the error type for better handling
        if _is_auth_error(e):
            raise LLMAuthenticationError(error_msg) from e
        elif _is_access_error(e):
            raise LLMAccessError(error_msg) from e
        else:
            raise LLMProviderError(error_msg) from e


def _create_anthropic_llm(config: Dict[str, Any]):
    """Create an Anthropic (Claude) client. Requires ``langchain-anthropic`` and
    ANTHROPIC_API_KEY. Claude has first-class tool/function calling, so the
    structured-output paths (with_structured_output) work natively."""
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise LLMProviderError(
            "langchain-anthropic not installed. Add it to the image "
            "(pip install langchain-anthropic) to use LLM_PROVIDER=anthropic."
        )

    api_key = config.get("api_key") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise LLMAuthenticationError(
            "ANTHROPIC_API_KEY not set. Get a key at https://console.anthropic.com"
        )

    return ChatAnthropic(
        model=config["model_id"],
        api_key=api_key,
        max_tokens=config.get("max_tokens", 4096),
    )


def _create_gemini_llm(config: Dict[str, Any]):
    """Create Gemini LLM instance."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = config.get("api_key") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise LLMAuthenticationError("GOOGLE_API_KEY or GEMINI_API_KEY not found in environment")

        return ChatGoogleGenerativeAI(
            model=config["model_id"],
            google_api_key=api_key,
            temperature=config["temperature"],
            convert_system_message_to_human=True,
        )
    except ImportError:
        raise LLMProviderError("langchain-google-genai not installed. Run 'pip install langchain-google-genai'")


def _is_auth_error(error: Exception) -> bool:
    """Check if error is authentication-related."""
    error_str = str(error).lower()
    auth_keywords = [
        "authentication",
        "unauthorized",
        "invalid credentials",
        "api key",
        "access key",
        "token",
        "permission denied",
        "403",
        "401",
    ]
    return any(keyword in error_str for keyword in auth_keywords)


def _is_access_error(error: Exception) -> bool:
    """Check if error is access/permission-related."""
    error_str = str(error).lower()
    access_keywords = [
        "access denied",
        "forbidden",
        "not authorized",
        "insufficient permissions",
        "quota exceeded",
        "rate limit",
        "service unavailable",
        "region not supported",
    ]
    return any(keyword in error_str for keyword in access_keywords)


def _get_helpful_error_message(provider: str, error: Exception) -> str:
    """Generate helpful error message based on provider and error type."""
    base_error = str(error)

    if provider == "anthropic":
        if _is_auth_error(error):
            return (
                f"Anthropic authentication failed: {base_error}\n"
                "Solutions:\n"
                "  1. Set ANTHROPIC_API_KEY environment variable\n"
                "  2. Check if your API key is valid at console.anthropic.com"
            )
        elif _is_access_error(error):
            return (
                f"Anthropic access error: {base_error}\n"
                "Solutions:\n"
                "  1. Verify ANTHROPIC_MODEL is available on your plan\n"
                "  2. Check rate limits / quotas in the Anthropic console"
            )

    if provider == "gemini":
        if _is_auth_error(error):
            return (
                f"Gemini authentication failed: {base_error}\n"
                "Solutions:\n"
                "  1. Set GOOGLE_API_KEY or GEMINI_API_KEY environment variable\n"
                "  2. Check if your API key is valid in Google AI Studio"
            )
        elif _is_access_error(error):
            return (
                f"Gemini access error: {base_error}\n"
                "Solutions:\n"
                "  1. Verify GEMINI_MODEL is available for your key\n"
                "  2. Check quotas in Google AI Studio"
            )

    return (
        f"{provider} provider error: {base_error}\n"
        "Solutions:\n"
        "  1. Check your network and API key\n"
        "  2. Verify the model name in your .env or constants.py"
    )


def validate_provider_access(provider: str = DEFAULT_PROVIDER, **kwargs) -> bool:
    """Validate if the specified provider is accessible.

    Args:
        provider: LLM provider to validate
        **kwargs: Additional configuration

    Returns:
        True if provider is accessible, False otherwise
    """
    if provider not in SUPPORTED_PROVIDERS:
        logger.warning(
            f"Unsupported provider: {provider}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )
        return False

    try:
        create_llm_with_error_handling(provider, **kwargs)
        logger.info(f"Provider {provider} validation successful")
        return True
    except Exception as e:
        logger.warning(f"Provider {provider} validation failed: {e}")
        return False


def create_llm_with_fallback(primary_provider: str | None = None, **kwargs):
    """Create LLM with automatic fallback: primary → other supported providers.

    Args:
        primary_provider: Provider to try first; defaults to LLM_PROVIDER or anthropic
        **kwargs: Additional configuration overrides

    Returns:
        LLM instance from the first successful provider

    Raises:
        LLMProviderError: If all providers fail
    """
    if primary_provider is None:
        primary_provider = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)

    primary_provider = require_supported_provider(primary_provider)

    provider_bound = any(
        kwargs.get(key) is not None for key in ("api_key", "model_id", "base_url")
    )
    fallback_chain = [primary_provider] if provider_bound else list(SUPPORTED_PROVIDERS)
    ordered = [primary_provider] + [p for p in fallback_chain if p != primary_provider]

    last_error = None
    for provider in ordered:
        try:
            llm = create_llm_with_error_handling(provider, **kwargs)
            if provider != primary_provider:
                logger.warning(
                    f"Fell back to provider '{provider}' (primary '{primary_provider}' failed)"
                )
            else:
                logger.info(f"Using provider '{provider}'")
            return llm
        except (LLMAuthenticationError, LLMAccessError) as e:
            logger.warning(f"Provider '{provider}' unavailable ({type(e).__name__}), trying next...")
            last_error = e
        except LLMProviderError as e:
            logger.warning(f"Provider '{provider}' failed ({e}), trying next...")
            last_error = e
        except Exception as e:
            logger.warning(f"Provider '{provider}' unexpected error ({e}), trying next...")
            last_error = e

    raise LLMProviderError(
        f"All LLM providers exhausted. Last error: {last_error}\n"
        "Check your API keys: ANTHROPIC_API_KEY and GOOGLE_API_KEY."
    )


def get_recommended_provider() -> str:
    """Get recommended provider based on availability.

    Returns:
        Recommended provider name
    """
    if validate_provider_access("anthropic"):
        logger.info("Recommended provider: anthropic")
        return "anthropic"

    if validate_provider_access("gemini"):
        logger.info("Recommended provider: gemini")
        return "gemini"

    logger.warning("No providers accessible. Defaulting to anthropic.")
    return DEFAULT_PROVIDER
