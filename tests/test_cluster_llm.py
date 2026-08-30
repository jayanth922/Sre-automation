#!/usr/bin/env python3
"""Per-cluster LLM authorization and runtime isolation."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from sre_agent.cluster_context import (
    UnauthorizedLLMConfigError,
    authorize_llm,
    llm_manifest,
    resolve_authorized_llm,
)
from sre_agent.execution_context import ExecutionContext
from sre_agent.runtime_cache import AgentRuntimeCache, RuntimeBundle


@pytest.fixture(autouse=True)
def _clean_llm_policy(monkeypatch):
    for key in (
        "ALLOWED_LLM_PROVIDERS",
        "ALLOWED_LLM_MODELS",
        "LLM_RUN_BUDGET",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "MODEL_ROUTER_STRONG_PROVIDER",
        "MODEL_ROUTER_STRONG_MODEL",
        "MODEL_ROUTER_BALANCED_PROVIDER",
        "MODEL_ROUTER_BALANCED_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_ROUTER_ENABLED", "true")


def _cluster(**overrides):
    base = dict(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        llm_provider=None,
        llm_model=None,
        llm_base_url=None,
        llm_api_key=None,
        namespace="ns-a",
        k8s_token=None,
        github_token=None,
        notion_api_key=None,
        key_version=1,
        execution_context_version=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_authorize_rejects_provider_outside_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "anthropic")
    with pytest.raises(UnauthorizedLLMConfigError, match="ALLOWED_LLM_PROVIDERS"):
        authorize_llm("gemini", model="gemini-2.0-flash")


def test_authorize_rejects_model_outside_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_LLM_MODELS", "claude-3-5-sonnet-latest")
    with pytest.raises(UnauthorizedLLMConfigError, match="ALLOWED_LLM_MODELS"):
        authorize_llm("anthropic", model="secret-finetune")


def test_authorize_rejects_exhausted_budget(monkeypatch):
    monkeypatch.setenv("LLM_RUN_BUDGET", "0")
    with pytest.raises(UnauthorizedLLMConfigError, match="exhausted"):
        authorize_llm("anthropic")


def test_from_cluster_resolves_authorized_effective_brain(monkeypatch):
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "gemini,anthropic")
    monkeypatch.setenv("ALLOWED_LLM_MODELS", "tenant-a-model,tenant-b-model")
    cluster = _cluster(
        llm_provider="gemini",
        llm_model="tenant-a-model",
        llm_base_url="https://tenant-a.example/v1",
        llm_api_key="tenant-a-key",
    )
    context = ExecutionContext.from_cluster(cluster)
    assert context.llm_manifest() == {
        "provider": "gemini",
        "model": "tenant-a-model",
        "base_url": "https://tenant-a.example/v1",
    }
    assert context.llm_kwargs()["api_key"] == "tenant-a-key"
    assert "tenant-a-key" not in context.llm_manifest().values()


@pytest.mark.asyncio
async def test_two_clusters_cache_separate_provider_model_runtimes(monkeypatch):
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "gemini,anthropic")
    monkeypatch.setenv(
        "ALLOWED_LLM_MODELS",
        "gemini-fast,claude-strong",
    )

    cluster_a = _cluster(llm_provider="gemini", llm_model="gemini-fast", namespace="ns-a")
    cluster_b = _cluster(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        llm_provider="anthropic",
        llm_model="claude-strong",
        namespace="ns-b",
    )
    context_a = ExecutionContext.from_cluster(cluster_a)
    context_b = ExecutionContext.from_cluster(cluster_b)
    assert context_a.fingerprint() != context_b.fingerprint()
    assert context_a.llm_manifest() != context_b.llm_manifest()
    assert llm_manifest(resolve_authorized_llm(cluster_a)) == context_a.llm_manifest()
    assert llm_manifest(resolve_authorized_llm(cluster_b)) == context_b.llm_manifest()

    created = []

    async def factory(context: ExecutionContext) -> RuntimeBundle:
        await asyncio.sleep(0)
        created.append(
            (
                context.cluster_id,
                context.llm_provider,
                context.llm_model,
                context.credentials.get("llm_api_key"),
            )
        )
        return RuntimeBundle(context, graph=object(), tools=[], mcp_client=None)

    cache = AgentRuntimeCache(max_size=8)
    bundle_a, bundle_b = await asyncio.gather(
        cache.get_or_create(context_a, factory),
        cache.get_or_create(context_b, factory),
    )
    again_a = await cache.get_or_create(context_a, factory)

    assert len(created) == 2
    assert {item[1] for item in created} == {"gemini", "anthropic"}
    assert {item[2] for item in created} == {"gemini-fast", "claude-strong"}
    assert bundle_a is again_a
    assert bundle_a is not bundle_b
    assert bundle_a.context.llm_manifest()["model"] == "gemini-fast"
    assert bundle_b.context.llm_manifest()["model"] == "claude-strong"


@pytest.mark.asyncio
async def test_runtime_accepts_cluster_scoped_provider_key(monkeypatch):
    from sre_agent import agent_runtime, checkpointer

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    context = ExecutionContext(
        organization_id="org-a",
        cluster_id="cluster-a",
        credentials={"llm_api_key": "tenant-anthropic-key"},
        namespace="ns-a",
        allowlist=("ns-a",),
        llm_provider="anthropic",
        environment="testing",
    )
    graph = object()

    async def fake_checkpointer():
        return object()

    async def fake_create(*args, **kwargs):
        return graph, [], None

    monkeypatch.setattr(checkpointer, "get_checkpointer", fake_checkpointer)
    monkeypatch.setattr(agent_runtime, "create_multi_agent_system", fake_create)

    bundle = await agent_runtime._build_runtime(context)

    assert bundle.context is context
    assert bundle.graph is graph
