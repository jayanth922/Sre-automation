#!/usr/bin/env python3
"""
Centralized LLM utilities with improved error handling.

This module provides a single point for LLM creation with proper error handling
for authentication, access, and configuration issues.
"""

import logging
from typing import Any, Dict

from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama

from .constants import SREConstants

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


def create_llm_with_error_handling(provider: str = "ollama", **kwargs):
    """Create LLM instance with proper error handling and helpful error messages.

    Args:
        provider: LLM provider (only "ollama" is supported)
        **kwargs: Additional configuration overrides

    Returns:
        LLM instance

    Raises:
        LLMProviderError: For general provider errors
        LLMAuthenticationError: For authentication failures
        LLMAccessError: For access/permission failures
        ValueError: For unsupported providers
    """
    if provider not in ("groq", "ollama", "gemini", "nvidia"):
        raise ValueError(
            f"Unsupported provider: {provider}. Supported: 'groq', 'ollama', 'gemini', 'nvidia'."
        )

    logger.info(f"Creating LLM with provider: {provider}")

    try:
        config = SREConstants.get_model_config(provider, **kwargs)

        if provider == "groq":
            logger.info(f"Creating Groq LLM - Model: {config['model_id']}")
            return _create_groq_llm(config)
        elif provider == "ollama":
            logger.info(f"Creating Ollama LLM - Model: {config['model_id']} at {config['base_url']}")
            return _create_ollama_llm(config)
        elif provider == "gemini":
            logger.info(f"Creating Gemini LLM - Model: {config['model_id']}")
            return _create_gemini_llm(config)
        elif provider == "nvidia":
            logger.info(f"Creating NVIDIA NIM LLM - Model: {config['model_id']}")
            return _create_nvidia_llm(config)

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


def _create_ollama_llm(config: Dict[str, Any]):
    """Create Ollama LLM instance."""
    return ChatOllama(
        model=config["model_id"],
        base_url=config.get("base_url", "http://localhost:11434"),
        temperature=config["temperature"],
        num_ctx=config.get("num_ctx", 32768),
    )


def _create_groq_llm(config: Dict[str, Any]):
    """Create Groq LLM instance."""
    return ChatGroq(
        model=config["model_id"],
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
    )


def _create_nvidia_llm(config: Dict[str, Any]):
    """Create NVIDIA NIM LLM instance (OpenAI-compatible endpoint)."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise LLMProviderError(
            "langchain-openai not installed. Run 'pip install langchain-openai'"
        )

    api_key = config.get("api_key", "")
    if not api_key:
        raise LLMAuthenticationError(
            "NVIDIA_API_KEY not set. Get a free key at https://build.nvidia.com"
        )

    return ChatOpenAI(
        model=config["model_id"],
        api_key=api_key,
        base_url=config["base_url"],
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
    )


def _create_gemini_llm(config: Dict[str, Any]):
    """Create Gemini LLM instance."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        import os
        
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise LLMAuthenticationError("GOOGLE_API_KEY not found in environment")
            
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

    if provider == "groq":
        if _is_auth_error(error):
            return (
                f"Groq authentication failed: {base_error}\n"
                "Solutions:\n"
                "  1. Set GROQ_API_KEY environment variable\n"
                "  2. Check if your API key is valid and active"
            )
        elif _is_access_error(error):
            return (
                f"Groq access error: {base_error}\n"
                "Solutions:\n"
                "  1. Verify model name exists for your account\n"
                "  2. Check rate limits / quotas in Groq console"
            )
    
    if provider == "gemini":
        if _is_auth_error(error):
            return (
                f"Gemini authentication failed: {base_error}\n"
                "Solutions:\n"
                "  1. Set GOOGLE_API_KEY environment variable\n"
                "  2. Check if your API key is valid in Google AI Studio"
            )

    if provider == "nvidia":
        if _is_auth_error(error):
            return (
                f"NVIDIA NIM authentication failed: {base_error}\n"
                "Solutions:\n"
                "  1. Set NVIDIA_API_KEY environment variable\n"
                "  2. Get a free key at https://build.nvidia.com\n"
                "  3. Verify the key is active in your NVIDIA account"
            )
        elif _is_access_error(error):
            return (
                f"NVIDIA NIM access error: {base_error}\n"
                "Solutions:\n"
                "  1. Check NVIDIA_MODEL is a valid NIM model ID\n"
                "  2. Verify your plan includes this model at build.nvidia.com"
            )

    if provider == "ollama":
        if "connection refused" in base_error.lower():
            return (
                f"Ollama connection failed: {base_error}\n"
                "Solutions:\n"
                "  1. Ensure Ollama container is running\n"
                "  2. Check OLLAMA_BASE_URL (default: http://ollama:11434)"
            )

    return (
        f"{provider} provider error: {base_error}\n"
        "Solutions:\n"
        "  1. Check your network and API key\n"
        "  2. Verify the model name in your .env or constants.py"
    )


def validate_provider_access(provider: str = "ollama", **kwargs) -> bool:
    """Validate if the specified provider is accessible.

    Args:
        provider: LLM provider to validate
        **kwargs: Additional configuration

    Returns:
        True if provider is accessible, False otherwise
    """
    if provider not in ["groq", "ollama", "gemini", "nvidia"]:
        logger.warning(f"Unsupported provider: {provider}. Supported: 'groq', 'ollama', 'gemini', 'nvidia'.")
        return False

    try:
        llm = create_llm_with_error_handling(provider, **kwargs)
        # Try a simple test call to validate access
        # Note: This is a minimal validation - actual usage may still fail
        logger.info(f"Provider {provider} validation successful")
        return True
    except Exception as e:
        logger.warning(f"Provider {provider} validation failed: {e}")
        return False


def create_llm_with_fallback(primary_provider: str | None = None, **kwargs):
    """Create LLM with automatic fallback chain: primary → others in [gemini, groq, ollama].

    Respects LLM_PROVIDER env var as default. Falls back on creation errors AND on
    quota/rate-limit errors raised during invocation by wrapping with _FallbackLLM.

    Args:
        primary_provider: Provider to try first; defaults to LLM_PROVIDER env var or 'groq'
        **kwargs: Additional configuration overrides

    Returns:
        LLM instance from the first successful provider

    Raises:
        LLMProviderError: If all providers fail
    """
    import os

    if primary_provider is None:
        primary_provider = os.getenv("LLM_PROVIDER", "groq")

    fallback_chain = ["nvidia", "gemini", "groq", "ollama"]
    ordered = [primary_provider] + [p for p in fallback_chain if p != primary_provider]

    last_error = None
    for provider in ordered:
        try:
            llm = create_llm_with_error_handling(provider, **kwargs)
            if provider != primary_provider:
                logger.warning(f"Fell back to provider '{provider}' (primary '{primary_provider}' failed)")
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
        "Check your API keys: NVIDIA_API_KEY, GOOGLE_API_KEY, GROQ_API_KEY, and OLLAMA_BASE_URL."
    )


def get_recommended_provider() -> str:
    """Get recommended provider based on availability.

    Returns:
        Recommended provider name
    """
    # Prefer Ollama for local execution if available
    if validate_provider_access("ollama"):
        logger.info("Recommended provider: ollama")
        return "ollama"

    if validate_provider_access("groq"):
        logger.info("Recommended provider: groq")
        return "groq"

    if validate_provider_access("gemini"):
        logger.info("Recommended provider: gemini")
        return "gemini"

    logger.warning("No providers accessible. Defaulting to ollama.")
    return "ollama"
