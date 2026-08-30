#!/usr/bin/env python3
"""R03 cluster-namespace scope helpers for execution and mutations."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, MutableMapping, Set

from .execution_context import ExecutionContext, is_production_runtime


_NAMESPACE_ARG_TOOLS = frozenset(
    {
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
)
_QUERY_TOOLS = frozenset(
    {
        "query_logs",
        "get_error_logs",
        "analyze_log_patterns",
        "get_metric",
        "get_metric_range",
        "get_golden_signals",
    }
)
_QUERY_ONLY_TOOLS = frozenset(
    {"query_logs", "analyze_log_patterns", "get_metric", "get_metric_range"}
)
_POSITIVE_NAMESPACE_SELECTOR = re.compile(
    r'namespace\s*(?:=|=~)\s*"([^"]*)"', re.IGNORECASE
)


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


def _scope_query(query: str, namespace: str) -> str:
    """Require one exact positive namespace selector in PromQL/LogQL."""
    matches = _POSITIVE_NAMESPACE_SELECTOR.findall(query)
    if any(value != namespace for value in matches):
        raise NamespaceScopeError(
            f"Query namespace selector is outside configured namespace '{namespace}'"
        )
    if matches:
        return _POSITIVE_NAMESPACE_SELECTOR.sub(
            f'namespace="{namespace}"', query
        )
    if "{" not in query:
        raise NamespaceScopeError(
            "Scoped metric/log query must include a label selector"
        )
    selector_start = query.index("{") + 1
    selector_end = query.find("}", selector_start)
    if selector_end < 0:
        raise NamespaceScopeError("Scoped metric/log query has an invalid selector")
    separator = "," if query[selector_start:selector_end].strip() else ""
    return (
        query[:selector_start]
        + f'namespace="{namespace}"{separator}'
        + query[selector_start:]
    )


def enforce_tool_arguments(
    tool_name: str,
    arguments: Any,
    context: ExecutionContext,
) -> Any:
    """Inject the authorized namespace and reject cross-namespace read calls."""
    name = (tool_name or "").strip()
    if name == "list_namespaces":
        raise NamespaceScopeError(
            "Listing cluster namespaces is unavailable in a tenant-scoped runtime"
        )
    if name not in _NAMESPACE_ARG_TOOLS:
        return arguments
    if not isinstance(arguments, Mapping):
        raise NamespaceScopeError(
            f"Tool '{name}' requires mapping arguments for namespace enforcement"
        )

    args = dict(arguments)
    allowed = allowed_namespaces(context)
    configured = require_cluster_namespace(context)
    supplied = str(args.get("namespace") or "").strip()
    if supplied and supplied not in allowed:
        raise NamespaceScopeError(
            f"Namespace '{supplied}' is outside cluster scope {sorted(allowed)}"
        )
    effective = configured or supplied
    if not effective:
        raise NamespaceScopeError(
            f"Tool '{name}' requires an explicit authorized namespace"
        )
    if name in _QUERY_ONLY_TOOLS:
        args.pop("namespace", None)
    else:
        args["namespace"] = effective

    if name in _QUERY_TOOLS:
        for key in ("query", "promql", "logql", "expr"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                args[key] = _scope_query(value, effective)
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
