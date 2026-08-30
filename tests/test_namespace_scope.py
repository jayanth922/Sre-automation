#!/usr/bin/env python3
"""Tests for R03 cluster-namespace enforcement."""

from types import SimpleNamespace

import pytest

from sre_agent.execution_context import ExecutionContext
from sre_agent.namespace_scope import (
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
        "namespace": "demo-app"
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
        "get_metric", {"query": 'rate(http_requests_total{service="api"}[5m])'}, _context()
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
            {"query": 'up{namespace="other-tenant"}'},
            _context(),
        )


def test_empty_query_selector_is_scoped_without_trailing_comma(monkeypatch):
    monkeypatch.setenv("REQUIRE_CLUSTER_NAMESPACE", "true")
    scoped = enforce_tool_arguments("query_logs", {"logql": '{} |= "error"'}, _context())
    assert scoped == {"logql": '{namespace="demo-app"} |= "error"'}


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
        llm_provider=None,
        llm_model=None,
        llm_base_url=None,
        key_version=1,
        execution_context_version=1,
    )
    with pytest.raises(NamespaceScopeError, match="no configured namespace"):
        ExecutionContext.from_cluster(cluster)
