#!/usr/bin/env python3
"""R03 cluster-namespace scope helpers for execution and mutations."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
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
        "get_deployment_spec",
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
        "sandbox_provision",
        "sandbox_status",
        "sandbox_logs",
        "sandbox_teardown",
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
_SCOPED_READ_TOOLS = frozenset(
    {*_NAMESPACE_ARG_TOOLS, "list_commits", "list_pull_requests", "search_runbooks"}
)
_POSITIVE_NAMESPACE_SELECTOR = re.compile(
    r'namespace\s*(?:=|=~)\s*"([^"]*)"', re.IGNORECASE
)
_SELECTOR_LABEL = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|=~)\s*"[^"]+"'
)
_TARGET_LABELS = frozenset(
    {"app", "service", "job", "pod", "container", "deployment", "instance"}
)
_RELATIVE_TIME = re.compile(r"^(\d+)([smhd])$")

MAX_INVESTIGATION_WINDOW = timedelta(minutes=30)
MAX_CODE_CHANGE_WINDOW = timedelta(hours=3)


class InvestigationQueryScopeError(ValueError):
    """A read was too broad to be useful or economical for one incident."""


def _parse_query_time(value: Any, *, now: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise InvestigationQueryScopeError("an explicit time value is required")
    if text.lower() == "now":
        return now
    relative = _RELATIVE_TIME.fullmatch(text.lower())
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        return now - timedelta(
            seconds=amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        )
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (ValueError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvestigationQueryScopeError(
            f"invalid time value {text!r}; use RFC3339, unix time, or a relative value"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _require_bounded_window(
    tool_name: str,
    args: MutableMapping[str, Any],
    *,
    start_key: str = "start_time",
    end_key: str = "end_time",
    max_window: timedelta = MAX_INVESTIGATION_WINDOW,
) -> None:
    now = datetime.now(timezone.utc)
    start = _parse_query_time(args.get(start_key), now=now)
    end = _parse_query_time(args.get(end_key), now=now)
    window = end - start
    if window.total_seconds() < 0:
        raise InvestigationQueryScopeError(
            f"{tool_name} end time must not precede start time"
        )
    if window > max_window:
        raise InvestigationQueryScopeError(
            f"{tool_name} window is {window}; maximum incident window is "
            f"{max_window}. Query the alert window first."
        )


def _require_targeted_query(tool_name: str, query: Any) -> None:
    text = str(query or "").strip()
    if not text:
        raise InvestigationQueryScopeError(f"{tool_name} requires a query")
    labels = {match.group(1).lower() for match in _SELECTOR_LABEL.finditer(text)}
    if not labels.intersection(_TARGET_LABELS):
        raise InvestigationQueryScopeError(
            f"{tool_name} requires an incident target selector such as "
            'service="...", app="...", job="...", or pod="..."'
        )


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _enforce_investigation_query_scope(
    tool_name: str, arguments: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Bound high-volume reads even when the model ignores its prompt."""
    args = dict(arguments)

    if tool_name in {"query_logs", "analyze_log_patterns"}:
        _require_targeted_query(tool_name, args.get("logql"))
        _require_bounded_window(tool_name, args)
        ceiling = 100 if tool_name == "query_logs" else 1000
        args["limit"] = min(_positive_int(args.get("limit"), ceiling), ceiling)
    elif tool_name == "get_error_logs":
        if not str(args.get("app") or "").strip():
            raise InvestigationQueryScopeError(
                "get_error_logs requires the affected app/service"
            )
        _require_bounded_window(tool_name, args)
        args["limit"] = min(_positive_int(args.get("limit"), 100), 100)
    elif tool_name == "get_metric":
        _require_targeted_query(tool_name, args.get("query"))
        _parse_query_time(args.get("time"), now=datetime.now(timezone.utc))
    elif tool_name == "get_metric_range":
        _require_targeted_query(tool_name, args.get("query"))
        _require_bounded_window(tool_name, args)
    elif tool_name == "get_golden_signals":
        if not str(args.get("service") or "").strip():
            raise InvestigationQueryScopeError(
                "get_golden_signals requires the affected service"
            )
        _parse_query_time(args.get("time"), now=datetime.now(timezone.utc))
    elif tool_name == "list_commits":
        _require_bounded_window(
            tool_name,
            args,
            start_key="since",
            end_key="until",
            max_window=MAX_CODE_CHANGE_WINDOW,
        )
        args["limit"] = min(_positive_int(args.get("limit"), 20), 20)
    elif tool_name == "list_pull_requests":
        args["limit"] = min(_positive_int(args.get("limit"), 10), 10)
    elif tool_name == "search_runbooks":
        alert_name = str(args.get("alert_name") or "").strip()
        runbook_id = str(args.get("runbook_id") or "").strip()
        service = str(args.get("service") or "").strip()
        incident_type = str(args.get("incident_type") or "").strip()
        if not (alert_name or runbook_id or (service and incident_type)):
            raise InvestigationQueryScopeError(
                "search_runbooks requires an exact alert name/runbook id or "
                "both affected service and incident type"
            )
    elif tool_name == "list_pods":
        if not str(args.get("label_selector") or "").strip():
            raise InvestigationQueryScopeError(
                "list_pods requires the affected workload label_selector"
            )
        args["limit"] = min(_positive_int(args.get("limit"), 50), 50)
    elif tool_name == "list_events":
        if not str(args.get("involved_object_name") or "").strip():
            raise InvestigationQueryScopeError(
                "list_events requires the affected pod or deployment name"
            )
        args["limit"] = min(_positive_int(args.get("limit"), 50), 50)
    elif tool_name == "get_pod_logs":
        args["tail_lines"] = min(
            _positive_int(args.get("tail_lines"), 100), 200
        )
    return args


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
    *,
    investigation_scope: bool | None = None,
) -> Any:
    """Apply tenant scope globally and bounded-read policy to investigators.

    Deterministic runtime callers (for example post-remediation verification)
    share the same MCP tools but are not model-directed searches. They retain
    namespace isolation without inheriting the specialists' time/limit gate.
    """
    if investigation_scope is None:
        from .audit_context import investigation_scope_active

        investigation_scope = investigation_scope_active()
    name = (tool_name or "").strip()
    if name == "list_namespaces":
        raise NamespaceScopeError(
            "Listing cluster namespaces is unavailable in a tenant-scoped runtime"
        )
    if not isinstance(arguments, Mapping):
        if name not in _SCOPED_READ_TOOLS:
            return arguments
        raise NamespaceScopeError(
            f"Tool '{name}' requires mapping arguments for namespace enforcement"
        )

    args = dict(arguments)
    if name not in _NAMESPACE_ARG_TOOLS:
        return (
            _enforce_investigation_query_scope(name, args)
            if investigation_scope
            else args
        )
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
    return (
        _enforce_investigation_query_scope(name, args)
        if investigation_scope
        else args
    )


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
