#!/usr/bin/env python3
"""Tenant execution-context and runtime-cache isolation tests."""

import asyncio
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from sre_agent.execution_context import ExecutionContext, require_execution_context
from sre_agent.runtime_cache import AgentRuntimeCache, RuntimeBundle


_ROOT = Path(__file__).resolve().parents[1]


def _context(cluster_id: str, endpoint: str, secret: str = "secret") -> ExecutionContext:
    return ExecutionContext(
        organization_id=f"org-{cluster_id}",
        cluster_id=cluster_id,
        mcp_endpoints={"metrics": endpoint},
        credentials={"llm_api_key": secret},
        namespace=f"ns-{cluster_id}",
        allowlist=(f"ns-{cluster_id}",),
    )


def test_context_from_cluster_uses_operator_mcp_routes_and_redacts_credentials(
    monkeypatch,
):
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")
    monkeypatch.setenv("MCP_K8S_URI", "https://operator.internal/k8s")
    monkeypatch.setenv("MCP_EXECUTOR_URI", "https://operator.internal/executor")
    cluster = SimpleNamespace(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        prometheus_url="https://tenant-a.example/mcp",
        loki_url="https://tenant-a.example/logs",
        k8s_api_server="https://tenant-a.example/k8s",
        k8s_token="k8s-super-secret",
        github_token=None,
        notion_api_key=None,
        llm_api_key="llm-super-secret",
        namespace="tenant-a",
        llm_provider="openai_compatible",
        llm_model="tenant-model",
        llm_base_url="https://tenant-a.example/v1",
        key_version=2,
        execution_context_version=4,
    )
    context = ExecutionContext.from_cluster(cluster)
    rendered = repr(context)

    assert context.organization_id == str(cluster.org_id)
    assert context.endpoint("metrics") == "https://operator.internal/metrics"
    assert cluster.prometheus_url not in context.mcp_endpoints.values()
    assert cluster.k8s_api_server not in context.mcp_endpoints.values()
    assert context.allowlist == ("tenant-a",)
    assert "k8s-super-secret" not in rendered
    assert "llm-super-secret" not in rendered


def test_fingerprint_contains_no_secret_and_changes_with_context_version():
    first = _context("a", "https://a.example/mcp", "first-secret")
    rotated = ExecutionContext(
        **{
            **first.__dict__,
            "credentials": {"llm_api_key": "second-secret"},
            "context_version": 2,
        }
    )
    assert "first-secret" not in first.fingerprint()
    assert first.fingerprint() != rotated.fingerprint()


def test_production_refuses_process_global_execution_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "api")
    with pytest.raises(RuntimeError, match="tenant-bound ExecutionContext"):
        require_execution_context(None)


def test_two_tenant_tool_callers_share_only_operator_route_and_keep_identity(
    monkeypatch,
):
    captured = []

    class FakeTool:
        name = "query"

        def __init__(self, endpoint):
            self.endpoint = endpoint

        async def ainvoke(self, args):
            await asyncio.sleep(0)
            return {"endpoint": self.endpoint, "args": args}

    class FakeClient:
        def __init__(self, config):
            self.config = config
            captured.append(config)

        async def get_tools(self):
            spec = next(iter(self.config.values()))
            return [FakeTool(spec["url"])]

    client_module = types.ModuleType("langchain_mcp_adapters.client")
    client_module.MultiServerMCPClient = FakeClient
    package_module = types.ModuleType("langchain_mcp_adapters")
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", package_module)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", client_module)
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "transport-secret")
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")

    from sre_agent.executor import build_metrics_tool_caller

    async def exercise():
        context_a = _context("a", "https://operator.internal/metrics")
        context_b = _context("b", "https://operator.internal/metrics")
        caller_a, caller_b = await asyncio.gather(
            build_metrics_tool_caller(context_a),
            build_metrics_tool_caller(context_b),
        )
        return await asyncio.gather(
            caller_a("query", {"tenant": "a"}),
            caller_b("query", {"tenant": "b"}),
        )

    result_a, result_b = asyncio.run(exercise())
    assert result_a["endpoint"] == "https://operator.internal/metrics"
    assert result_b["endpoint"] == "https://operator.internal/metrics"
    assert captured[0]["metrics"]["headers"]["Authorization"] == (
        "Bearer transport-secret"
    )
    assert captured[0]["metrics"]["headers"]["X-Sentinel-Cluster-ID"] != (
        captured[1]["metrics"]["headers"]["X-Sentinel-Cluster-ID"]
    )


def test_tenant_controlled_endpoint_is_rejected_before_token_attachment(monkeypatch):
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "transport-secret")
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")

    from sre_agent.executor import build_metrics_tool_caller

    attacker_context = _context("attacker", "https://attacker.example/steal")
    with pytest.raises(RuntimeError, match="not operator-controlled"):
        asyncio.run(build_metrics_tool_caller(attacker_context))


def test_unknown_operator_environment_defaults_to_production(monkeypatch):
    monkeypatch.setenv("SENTINEL_CLUSTER_ENVIRONMENT", "tenant-namespace")
    assert ExecutionContext.from_environment().environment == "production"


def test_runtime_cache_deduplicates_concurrent_builds_and_closes_evictions():
    builds = []
    closed = []

    class Client:
        def __init__(self, cluster_id):
            self.cluster_id = cluster_id

        async def aclose(self):
            closed.append(self.cluster_id)

    async def factory(context):
        builds.append(context.cluster_id)
        await asyncio.sleep(0)
        return RuntimeBundle(context, object(), [], Client(context.cluster_id))

    async def exercise():
        cache = AgentRuntimeCache(max_size=1)
        context_a = _context("a", "https://a.example/mcp")
        context_b = _context("b", "https://b.example/mcp")
        first, duplicate = await asyncio.gather(
            cache.get_or_create(context_a, factory),
            cache.get_or_create(context_a, factory),
        )
        assert first is duplicate
        await cache.get_or_create(context_b, factory)
        await cache.close_all()

    asyncio.run(exercise())
    assert builds.count("a") == 1
    assert builds.count("b") == 1
    assert closed == ["a", "b"]


def test_agent_runtime_uses_context_cache_not_process_singletons():
    source = (_ROOT / "sre_agent" / "agent_runtime.py").read_text()
    assert "AgentRuntimeCache(" in source
    assert "_runtime_cache.get_or_create" in source
    assert "mcp_client_global" not in source
    assert "global agent_graph" not in source
    assert "agent_graph = None" not in source
