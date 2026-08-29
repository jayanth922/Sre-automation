#!/usr/bin/env python3
"""R03 cluster-namespace scope helpers for tool calls and mutations."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, MutableMapping, Optional, Set

from .execution_context import ExecutionContext, is_production_runtime

# Tools whose arguments are namespaced Kubernetes / observability lookups.
_NAMESPACE_ARG_TOOLS = {
    "get_pod_status",
    "list_pods",
    "list_services",
    "list_deployments",
    "list_events",
    "get_service_endpoints",
    "get_pod_logs",
    "get_deployment_status",
    "restart_deployment",
    "scale_deployment",
    "rollback_deployment",
    "patch_resource_limits",
    "query_logs",
    "get_error_logs",
    "analyze_log_patterns",
    "get_metric",
    "get_metric_range",
    "get_golden_signals",
}

_QUERY_TOOLS = {
    "get_metric",
    "get_metric_range",
    "get_golden_signals",
    "query_logs",
    "get_error_logs",
    "analyze_log_patterns",
}

_NAMESPACE_SELECTOR = re.compile(
    r'(namespace\s*=\s*")([^"]*)(")',
    re.IGNORECASE,
)


class NamespaceScopeError(PermissionError):
    """A tool or action attempted to leave the authorized cluster namespace."""


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


def _rewrite_query(query: str, namespace: str) -> str:
    if "namespace=" in query.lower():
        return _NAMESPACE_SELECTOR.sub(rf"\1{namespace}\3", query)
    if "{" in query:
        return query.replace("{", '{namespace="' + namespace + '",', 1)
    return query


def enforce_tool_arguments(
    tool_name: str,
    arguments: Optional[Mapping[str, Any]],
    context: ExecutionContext,
) -> dict[str, Any]:
    """Inject the configured namespace and reject cross-namespace targets."""
    args: dict[str, Any] = dict(arguments or {})
    name = (tool_name or "").strip()
    allowed = allowed_namespaces(context)
    required = require_cluster_namespace(context)

    if name == "list_namespaces":
        if required:
            args["namespace"] = required
        return args

    if name not in _NAMESPACE_ARG_TOOLS:
        return args

    if not allowed:
        raise NamespaceScopeError(
            f"Tool '{name}' requires a configured cluster namespace"
        )

    supplied = str(args.get("namespace") or "").strip()
    if supplied and supplied not in allowed:
        raise NamespaceScopeError(
            f"Namespace '{supplied}' is outside cluster scope {sorted(allowed)}"
        )
    if required:
        args["namespace"] = required

    if name in _QUERY_TOOLS and required:
        for key in ("query", "promql", "logql", "expr"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                args[key] = _rewrite_query(value, required)

    return args


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
