#!/usr/bin/env python3
"""Tests for R03 cluster-namespace enforcement."""

from types import SimpleNamespace

import pytest

from sre_agent.execution_context import ExecutionContext
from sre_agent.namespace_scope import (
    NamespaceScopeError,
    assert_action_namespace,
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
