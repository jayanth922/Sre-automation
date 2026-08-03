"""Resolve a cluster's effective runtime context with a platform-default
fallback — the single source of truth for per-cluster overrides.

Only auth/org is platform-wide; everything a client brings (LLM brain, scope,
integration credentials) resolves per-cluster here, falling back to the platform
default when a cluster hasn't set an override.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _attr(cluster: Any, name: str) -> Optional[str]:
    val = getattr(cluster, name, None) if cluster is not None else None
    return val if (val is not None and str(val).strip() != "") else None


def resolve_llm(cluster: Any) -> Dict[str, Optional[str]]:
    """Per-cluster LLM brain with platform-default fallback.

    provider is always resolved (cluster override → platform env → groq).
    model/base_url/api_key are None unless the cluster overrides them, in which
    case the router/factory should prefer them over the platform defaults.
    `cluster` may be None → platform defaults.
    """
    return {
        "provider": _attr(cluster, "llm_provider") or os.getenv("LLM_PROVIDER", "groq"),
        "model": _attr(cluster, "llm_model"),
        "base_url": _attr(cluster, "llm_base_url"),
        "api_key": _attr(cluster, "llm_api_key"),
    }


def resolve_namespace(cluster: Any) -> Optional[str]:
    """The cluster's namespace scope, or None for whole-cluster (infra) scope."""
    return _attr(cluster, "namespace")
