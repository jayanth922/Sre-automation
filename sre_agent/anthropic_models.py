"""Live Anthropic model listing for the Agent brain settings UI."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicModelsError(Exception):
    """Raised when the Anthropic models list cannot be fetched.

    Args:
        message: Human-readable explanation.
        status_code: Suggested HTTP status for the caller to surface (400 for
            a bad/missing key, 502 for an unreachable upstream).
    """

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


async def fetch_anthropic_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, str]]:
    """Fetch the list of models available to this Anthropic API key."""
    if not api_key or not api_key.strip():
        raise AnthropicModelsError("An Anthropic API key is required", status_code=400)

    base = (base_url or ANTHROPIC_API_BASE).rstrip("/")
    headers = {
        "x-api-key": api_key.strip(),
        "anthropic-version": ANTHROPIC_VERSION,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        raise AnthropicModelsError(
            f"Could not reach Anthropic API: {exc}", status_code=502
        ) from exc

    if response.status_code == 401:
        raise AnthropicModelsError("Invalid Anthropic API key", status_code=400)
    if response.status_code >= 400:
        raise AnthropicModelsError(
            f"Anthropic API returned {response.status_code}: {response.text[:200]}",
            status_code=502,
        )

    try:
        payload: Dict[str, Any] = response.json()
    except ValueError as exc:
        raise AnthropicModelsError(
            "Anthropic API returned an invalid response", status_code=502
        ) from exc

    models = payload.get("data", [])
    return [
        {"id": model["id"], "display_name": model.get("display_name", model["id"])}
        for model in models
        if model.get("id")
    ]
