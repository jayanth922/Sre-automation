#!/usr/bin/env python3
"""Tests for R03 cluster-namespace enforcement."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from sre_agent.execution_context import ExecutionContext
from sre_agent.audit_context import clear_audit_context, set_audit_context
from sre_agent.mcp_tool_wrapper import wrap_tool_with_namespace_scope
from sre_agent.namespace_scope import (
    InvestigationQueryScopeError,
    NamespaceScopeError,
    assert_action_namespace,
    enforce_tool_arguments,
    require_cluster_namespace,
)


def _context(namespace: str | None = "demo-app") -> ExecutionContext:
    return ExecutionContext(
        organization_id="org-1",
        cluster_id="cluster-1",
        mcp_endpoints={"k8s": "http://k8s"},
        namespace=namespace,
        allowlist=(namespace,) if namespace else (),
        environment="testing",
    )


def test_api_runtime_requires_cluster_namespace(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    with pytest.raises(NamespaceScopeError, match="required"):
        require_cluster_namespace(_context(None))


def test_missing_action_namespace_is_injected(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    action = SimpleNamespace(parameters={})
    assert_action_namespace(action, _context("demo-app"))
    assert action.parameters["namespace"] == "demo-app"


def test_cross_namespace_action_is_rejected(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    action = SimpleNamespace(parameters={"namespace": "other-tenant"})
    with pytest.raises(NamespaceScopeError, match="outside cluster scope"):
        assert_action_namespace(action, _context("demo-app"))


def test_read_tool_namespace_is_injected(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    assert enforce_tool_arguments("list_pods", {}, _context()) == {
        "namespace": "demo-app",
    }


def test_cross_namespace_read_is_rejected(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    with pytest.raises(NamespaceScopeError, match="outside cluster scope"):
        enforce_tool_arguments(
            "list_pods", {"namespace": "other-tenant"}, _context()
        )


def test_namespace_enumeration_is_rejected():
    with pytest.raises(NamespaceScopeError, match="Listing cluster namespaces"):
        enforce_tool_arguments("list_namespaces", {}, _context())


def test_metric_query_is_scoped_to_exact_namespace(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    scoped = enforce_tool_arguments(
        "get_metric",
        {
            "query": 'rate(http_requests_total{service="api"}[5m])',
            "time": "2026-09-19T10:00:00Z",
        },
        _context(),
        investigation_scope=True,
    )
    assert scoped["query"] == (
        'rate(http_requests_total{namespace="demo-app",service="api"}[5m])'
    )
    assert "namespace" not in scoped


def test_cross_namespace_metric_query_is_rejected(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    with pytest.raises(NamespaceScopeError, match="outside configured namespace"):
        enforce_tool_arguments(
            "get_metric",
            {
                "query": 'up{namespace="other-tenant",service="api"}',
                "time": "2026-09-19T10:00:00Z",
            },
            _context(),
            investigation_scope=True,
        )


def test_targeted_log_query_is_scoped_and_bounded(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    scoped = enforce_tool_arguments(
        "query_logs",
        {
            "logql": '{app="checkout-service"} |= "error"',
            "start_time": "2026-09-19T09:55:00Z",
            "end_time": "2026-09-19T10:05:00Z",
            "limit": 9999,
        },
        _context(),
        investigation_scope=True,
    )
    assert scoped == {
        "logql": '{namespace="demo-app",app="checkout-service"} |= "error"',
        "start_time": "2026-09-19T09:55:00Z",
        "end_time": "2026-09-19T10:05:00Z",
        "limit": 100,
    }


def test_log_query_rejects_namespace_only_or_missing_time():
    with pytest.raises(InvestigationQueryScopeError, match="target selector"):
        enforce_tool_arguments(
            "query_logs",
            {
                "logql": '{} |= "error"',
                "start_time": "2026-09-19T09:55:00Z",
                "end_time": "2026-09-19T10:05:00Z",
            },
            _context(),
            investigation_scope=True,
        )
    with pytest.raises(InvestigationQueryScopeError, match="time value"):
        enforce_tool_arguments(
            "query_logs",
            {"logql": '{app="checkout-service"} |= "error"'},
            _context(),
            investigation_scope=True,
        )


def test_log_and_metric_ranges_reject_open_ended_windows():
    for tool_name, query_key, query in (
        ("query_logs", "logql", '{app="checkout-service"}'),
        ("get_metric_range", "query", 'up{service="checkout-service"}'),
    ):
        with pytest.raises(InvestigationQueryScopeError, match="maximum"):
            enforce_tool_arguments(
                tool_name,
                {
                    query_key: query,
                    "start_time": "2026-09-19T08:00:00Z",
                    "end_time": "2026-09-19T10:00:00Z",
                },
                _context(),
                investigation_scope=True,
            )


def test_metrics_require_target_and_alert_time():
    with pytest.raises(InvestigationQueryScopeError, match="target selector"):
        enforce_tool_arguments(
            "get_metric",
            {
                "query": 'min(payment_provider_up{namespace="demo-app"})',
                "time": "now",
            },
            _context(),
            investigation_scope=True,
        )
    with pytest.raises(InvestigationQueryScopeError, match="time value"):
        enforce_tool_arguments(
            "get_metric",
            {"query": 'up{service="checkout-service"}'},
            _context(),
            investigation_scope=True,
        )


def test_commit_search_requires_bounded_incident_window_and_small_limit():
    scoped = enforce_tool_arguments(
        "list_commits",
        {
            "since": "2026-09-19T08:00:00Z",
            "until": "2026-09-19T10:15:00Z",
            "limit": 100,
        },
        _context(),
        investigation_scope=True,
    )
    assert scoped["limit"] == 20
    with pytest.raises(InvestigationQueryScopeError, match="maximum"):
        enforce_tool_arguments(
            "list_commits",
            {
                "since": "2026-09-18T08:00:00Z",
                "until": "2026-09-19T10:15:00Z",
            },
            _context(),
            investigation_scope=True,
        )


def test_deterministic_verification_is_not_subject_to_specialist_query_gate():
    scoped = enforce_tool_arguments(
        "get_metric",
        {"query": 'ALERTS{service="checkout-service"}'},
        _context(),
    )
    assert scoped == {
        "query": 'ALERTS{namespace="demo-app",service="checkout-service"}'
    }


def test_specialist_audit_context_activates_investigation_query_gate():
    set_audit_context(investigation_scope=True)
    try:
        with pytest.raises(InvestigationQueryScopeError, match="time value"):
            enforce_tool_arguments(
                "query_logs",
                {"logql": '{app="checkout-service"} |= "error"'},
                _context(),
            )
    finally:
        clear_audit_context()


def test_specialist_kubernetes_reads_require_the_alert_target():
    with pytest.raises(InvestigationQueryScopeError, match="label_selector"):
        enforce_tool_arguments(
            "list_pods", {}, _context(), investigation_scope=True
        )
    with pytest.raises(InvestigationQueryScopeError, match="pod or deployment"):
        enforce_tool_arguments(
            "list_events", {}, _context(), investigation_scope=True
        )

    scoped = enforce_tool_arguments(
        "list_pods",
        {"label_selector": "app=checkout-service", "limit": 500},
        _context(),
        investigation_scope=True,
    )
    assert scoped == {
        "namespace": "demo-app",
        "label_selector": "app=checkout-service",
        "limit": 50,
    }


def test_specialist_runbook_search_requires_structured_incident_scope():
    with pytest.raises(InvestigationQueryScopeError, match="exact alert name"):
        enforce_tool_arguments(
            "search_runbooks",
            {"query": "errors"},
            _context(),
            investigation_scope=True,
        )

    assert enforce_tool_arguments(
        "search_runbooks",
        {
            "alert_name": "CheckoutHighErrorRate",
            "service": "checkout-service",
            "incident_type": "high_error_rate",
        },
        _context(),
        investigation_scope=True,
    )["alert_name"] == "CheckoutHighErrorRate"


def test_from_cluster_fails_closed_without_namespace(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    cluster = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        org_id="22222222-2222-2222-2222-222222222222",
        namespace=None,
        k8s_token=None,
        github_token=None,
        notion_api_key=None,
        llm_api_key=None,
        # Explicit provider: isolates this test to the namespace check. A
        # cluster with no llm_provider now fails closed on that first (no
        # silent platform-default substitution — see test_cluster_llm.py).
        llm_provider="anthropic",
        llm_model=None,
        llm_base_url=None,
        key_version=1,
        execution_context_version=1,
    )
    with pytest.raises(NamespaceScopeError, match="no configured namespace"):
        ExecutionContext.from_cluster(cluster)


# --- the wrapper that feeds this gate under LangGraph ------------------------
#
# Every test above calls enforce_tool_arguments with a flat mapping, which is
# why the wrapper's envelope bug survived into production: a ToolNode invokes
# a tool with the whole ToolCall, so the gate was reading {"name","args","id",
# "type"} and finding none of the keys it checks.


class _RecordingTool:
    """Minimal stand-in for a LangChain tool; records what it was invoked with."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.received: Any = None

    def invoke(self, payload, config=None, **kwargs):
        self.received = payload
        return "ok"

    async def ainvoke(self, payload, config=None, **kwargs):
        self.received = payload
        return "ok"


def _tool_call(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": "call_abc123", "type": "tool_call"}


def test_tool_call_envelope_is_unwrapped_before_enforcement(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("list_pods")
    wrap_tool_with_namespace_scope(tool, _context())
    runtime = object()  # LangGraph injects this alongside the model's args

    set_audit_context(investigation_scope=True)
    try:
        assert tool.invoke(
            _tool_call(
                "list_pods",
                {"label_selector": "app=checkout-service", "runtime": runtime},
            )
        ) == "ok"
    finally:
        clear_audit_context()

    # The envelope survives intact — ToolNode matches the result by id — and
    # the tenant namespace lands on the arguments, not beside them.
    assert tool.received["id"] == "call_abc123"
    assert tool.received["type"] == "tool_call"
    assert tool.received["name"] == "list_pods"
    assert tool.received["args"] == {
        "label_selector": "app=checkout-service",
        "runtime": runtime,
        "namespace": "demo-app",
        "limit": 50,
    }


def test_tool_call_envelope_scopes_the_query_not_the_envelope(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("get_metric")
    wrap_tool_with_namespace_scope(tool, _context())

    tool.invoke(_tool_call("get_metric", {"query": 'up{service="checkout-service"}'}))

    assert tool.received["args"]["query"] == (
        'up{namespace="demo-app",service="checkout-service"}'
    )
    assert "namespace" not in tool.received  # never written onto the envelope


def test_tool_call_envelope_still_refuses_a_cross_tenant_read(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("get_pod_status")
    wrap_tool_with_namespace_scope(tool, _context())

    with pytest.raises(NamespaceScopeError, match="outside cluster scope"):
        tool.invoke(
            _tool_call("get_pod_status", {"namespace": "other-tenant", "pod": "p-1"})
        )
    assert tool.received is None


def test_async_tool_call_envelope_is_unwrapped(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("get_deployment_status")
    wrap_tool_with_namespace_scope(tool, _context())

    asyncio.run(
        tool.ainvoke(_tool_call("get_deployment_status", {"name": "checkout"}))
    )

    assert tool.received["args"] == {"name": "checkout", "namespace": "demo-app"}
    assert tool.received["id"] == "call_abc123"


def test_flat_arguments_are_still_enforced_directly(monkeypatch):
    """Direct callers (deterministic verification) pass arguments, not a ToolCall."""
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("list_pods")
    wrap_tool_with_namespace_scope(tool, _context())

    tool.invoke({"label_selector": "app=checkout-service"})

    assert tool.received == {
        "label_selector": "app=checkout-service",
        "namespace": "demo-app",
    }


def test_a_real_args_parameter_is_not_mistaken_for_an_envelope(monkeypatch):
    """A tool whose own schema has an "args" field must not be unwrapped."""
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    tool = _RecordingTool("get_pod_logs")
    wrap_tool_with_namespace_scope(tool, _context())

    tool.invoke({"pod": "checkout-1", "args": {"container": "app"}})

    assert tool.received["pod"] == "checkout-1"
    assert tool.received["args"] == {"container": "app"}
    assert tool.received["namespace"] == "demo-app"
