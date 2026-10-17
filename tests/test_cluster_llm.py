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
    resolve_llm,
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


def test_resolve_llm_no_platform_default_for_bound_cluster(monkeypatch):
    """A real cluster with no llm_provider override gets None, never LLM_PROVIDER env."""
    monkeypatch.setenv("LLM_PROVIDER", "other-provider")
    cluster = _cluster()
    assert resolve_llm(cluster)["provider"] is None


def test_resolve_llm_env_fallback_only_with_no_cluster(monkeypatch):
    """cluster=None means local dev / self-hosted single-tenant mode: env applies."""
    monkeypatch.setenv("LLM_PROVIDER", "other-provider")
    assert resolve_llm(None)["provider"] == "other-provider"


def test_authorize_rejects_unconfigured_provider():
    with pytest.raises(UnauthorizedLLMConfigError, match="No LLM provider configured"):
        authorize_llm(None)


def test_resolve_authorized_llm_fails_closed_for_unconfigured_cluster():
    """Fails closed even though the autouse fixture leaves LLM_PROVIDER=anthropic
    set — a bound cluster must never inherit that as a silent default."""
    cluster = _cluster()
    with pytest.raises(UnauthorizedLLMConfigError, match="No LLM provider configured"):
        resolve_authorized_llm(cluster)


def test_authorize_rejects_unsupported_provider():
    with pytest.raises(UnauthorizedLLMConfigError, match="Unsupported LLM provider"):
        authorize_llm("gemini", model="gemini-2.0-flash")


def test_authorize_rejects_provider_outside_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "groq")
    with pytest.raises(UnauthorizedLLMConfigError, match="ALLOWED_LLM_PROVIDERS"):
        authorize_llm("anthropic", model="claude-3-5-sonnet-latest")


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
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "anthropic")
    monkeypatch.setenv("ALLOWED_LLM_MODELS", "tenant-a-model,tenant-b-model")
    cluster = _cluster(
        llm_provider="anthropic",
        llm_model="tenant-a-model",
        llm_base_url="https://tenant-a.example/v1",
        llm_api_key="tenant-a-key",
    )
    context = ExecutionContext.from_cluster(cluster)
    assert context.llm_manifest() == {
        "provider": "anthropic",
        "model": "tenant-a-model",
        "base_url": "https://tenant-a.example/v1",
    }
    assert context.llm_kwargs()["api_key"] == "tenant-a-key"
    assert "tenant-a-key" not in context.llm_manifest().values()


@pytest.mark.asyncio
async def test_two_clusters_cache_separate_model_runtimes(monkeypatch):
    monkeypatch.setenv("MCP_METRICS_URI", "https://operator.internal/metrics")
    monkeypatch.setenv("ALLOWED_LLM_PROVIDERS", "anthropic")
    monkeypatch.setenv(
        "ALLOWED_LLM_MODELS",
        "claude-fast,claude-strong",
    )

    cluster_a = _cluster(llm_provider="anthropic", llm_model="claude-fast", namespace="ns-a")
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
    assert {item[1] for item in created} == {"anthropic"}
    assert {item[2] for item in created} == {"claude-fast", "claude-strong"}
    assert bundle_a is again_a
    assert bundle_a is not bundle_b
    assert bundle_a.context.llm_manifest()["model"] == "claude-fast"
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


# ── Dynamic routing: a routed cluster has no static model ─────────────────────


def _clear_tier_overrides(monkeypatch):
    for tier in ("FAST", "BALANCED", "STRONG"):
        for suffix in ("MODEL", "PROVIDER", "LITELLM_MODEL"):
            monkeypatch.delenv(f"MODEL_ROUTER_{tier}_{suffix}", raising=False)


def test_routed_cluster_resolves_no_static_model():
    """A model saved before routing was switched on must not reach the runtime."""
    cluster = _cluster(
        llm_provider="anthropic", llm_model="claude-opus-5", llm_router_enabled=True
    )
    assert resolve_llm(cluster)["model"] is None
    assert resolve_authorized_llm(cluster)["model"] is None


def test_unrouted_cluster_keeps_its_static_model():
    cluster = _cluster(
        llm_provider="anthropic", llm_model="claude-opus-5", llm_router_enabled=False
    )
    assert resolve_llm(cluster)["model"] == "claude-opus-5"


def test_routed_cluster_picks_per_task_not_the_stale_model(monkeypatch):
    """From the cluster row through ExecutionContext to the router's pick.

    The stale model used to become the router's anchor, and BALANCED returns
    the anchor verbatim: every specialist ran on the saved model.
    """
    from sre_agent.model_router import TaskType, select_model

    _clear_tier_overrides(monkeypatch)
    cluster = _cluster(
        llm_provider="anthropic", llm_model="claude-opus-5", llm_router_enabled=True
    )
    context = ExecutionContext.from_cluster(cluster)
    assert context.llm_router_enabled is True
    assert context.llm_kwargs().get("model_id") is None
    assert context.llm_manifest()["model"] is None

    def pick(task_type):
        return select_model(
            task_type,
            provider=context.llm_provider,
            router_enabled=context.llm_router_enabled,
            anchor_model=context.llm_model,
        ).model_id

    assert pick(TaskType.SPECIALIST) == "claude-sonnet-5"
    assert pick(TaskType.ROUTING) == "claude-haiku-4-5-20251001"
    assert pick(TaskType.REFLECTION) == "claude-opus-5"


class _Session:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, _obj):
        pass


def _update(monkeypatch, cluster, **fields):
    from backend import crud, schemas

    async def owned(_db, _cluster_id):
        return cluster

    monkeypatch.setattr(crud, "get_cluster_by_id", owned)
    return asyncio.run(
        crud.update_cluster(
            _Session(), cluster.id, cluster.org_id, schemas.ClusterUpdate(**fields)
        )
    )


def test_update_turning_routing_on_drops_the_model_sent_with_it(monkeypatch):
    cluster = _cluster(llm_router_enabled=False)
    updated = _update(
        monkeypatch,
        cluster,
        llm_provider="anthropic",
        llm_model="claude-opus-5",
        llm_router_enabled=True,
    )
    assert updated.llm_router_enabled is True
    assert updated.llm_model is None
    assert updated.execution_context_version == 2


def test_update_clears_a_model_left_from_before_routing(monkeypatch):
    """A save that does not touch the LLM fields still clears the stale row."""
    cluster = _cluster(
        llm_provider="anthropic", llm_model="claude-opus-5", llm_router_enabled=True
    )
    updated = _update(monkeypatch, cluster, name="renamed")
    assert updated.llm_model is None
    assert updated.execution_context_version == 2


def test_update_with_routing_off_keeps_the_chosen_model(monkeypatch):
    cluster = _cluster(llm_router_enabled=False)
    updated = _update(
        monkeypatch, cluster, llm_provider="anthropic", llm_model="claude-opus-5"
    )
    assert updated.llm_model == "claude-opus-5"


def test_create_with_routing_on_stores_no_model():
    from backend import crud, schemas

    db = _Session()
    created, _token = asyncio.run(
        crud.create_cluster(
            db,
            schemas.ClusterCreate(
                name="routed",
                llm_provider="anthropic",
                llm_model="claude-opus-5",
                llm_router_enabled=True,
            ),
            uuid.uuid4(),
        )
    )
    assert created.llm_router_enabled is True
    assert created.llm_model is None
