#!/usr/bin/env python3
"""R03 cluster-namespace scope helpers for execution and mutations."""

from __future__ import annotations

import os
from typing import Any, MutableMapping, Set

from .execution_context import ExecutionContext, is_production_runtime


class NamespaceScopeError(PermissionError):
    """An operation attempted to leave the authorized cluster namespace."""


def namespace_required() -> bool:
    configured = os.getenv("REQUIRE_CLUSTER_NAMESPACE", "").strip().lower()
    if configured in {"1", "true", "yes"}:
        return True
    if configured in {"0", "false", "no"}:
        return False
    return is_production_runtime() or os.getenv("AGENT_MODE", "").lower() == "api"


def allowed_namespaces(context: ExecutionContext) -> Set[str]:
    allowed = {item for item in context.allowlist if item}
    if context.namespace:
        allowed.add(context.namespace)
    return allowed


def require_cluster_namespace(context: ExecutionContext) -> str:
    """Fail closed when multi-tenant/API runtime has no configured namespace."""
    namespace = (context.namespace or "").strip()
    if namespace:
        return namespace
    if namespace_required():
        raise NamespaceScopeError(
            "Cluster namespace is required for scoped investigations and mutations"
        )
    return ""


def assert_action_namespace(action: Any, context: ExecutionContext) -> None:
    """Reject remediation actions that omit or leave the configured namespace."""
    allowed = allowed_namespaces(context)
    required = require_cluster_namespace(context)
    params = getattr(action, "parameters", None) or {}
    if not isinstance(params, MutableMapping) and not isinstance(params, dict):
        raise NamespaceScopeError("Action parameters must be a mapping")
    params = dict(params)
    supplied = str(params.get("namespace") or "").strip()
    if not allowed and namespace_required():
        raise NamespaceScopeError("No mutation namespace is configured")
    if supplied and allowed and supplied not in allowed:
        raise NamespaceScopeError(
            f"Namespace '{supplied}' is outside cluster scope {sorted(allowed)}"
        )
    if required and not supplied:
        params["namespace"] = required
        if hasattr(action, "parameters"):
            try:
                action.parameters = params
            except Exception:
                pass
