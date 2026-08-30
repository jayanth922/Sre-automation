"""Layered integration fixtures for two tenants, fake LLM, and fake MCP tools."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

# Keep backend import-time DB construction deterministic in CI/local.
for _k, _v in {
    "SECRET_KEY": "ci-secret-key",
    "POSTGRES_USER": "sentinel",
    "POSTGRES_PASSWORD": "sentinel",
    "POSTGRES_DB": "sentinel",
    "POSTGRES_HOST": "localhost",
    "LLM_PROVIDER": "groq",
    "LIVE_BUS_BACKEND": "memory",
}.items():
    os.environ.setdefault(_k, _v)


@dataclass(frozen=True)
class Tenant:
    org_id: str
    user_id: str
    cluster_id: str
    name: str


@dataclass
class TwinTenants:
    a: Tenant
    b: Tenant


@dataclass
class FakeLLM:
    """Deterministic LLM stand-in: returns canned text, records prompts."""

    responses: List[str] = field(default_factory=lambda: ["ok"])
    calls: List[str] = field(default_factory=list)
    _idx: int = 0

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            return ""
        out = self.responses[min(self._idx, len(self.responses) - 1)]
        self._idx += 1
        return out


@dataclass
class FakeMCPTool:
    """Minimal tool object compatible with wrap_tool_with_retry."""

    name: str
    side_effects: List[Any] = field(default_factory=list)
    calls: int = 0

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        if not self.side_effects:
            return {"ok": True, "tool": self.name}
        effect = self.side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.invoke(*_args, **_kwargs)


@pytest.fixture
def twin_tenants() -> TwinTenants:
    return TwinTenants(
        a=Tenant(
            org_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            cluster_id=str(uuid.uuid4()),
            name="tenant-a",
        ),
        b=Tenant(
            org_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            cluster_id=str(uuid.uuid4()),
            name="tenant-b",
        ),
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(responses=["investigation complete", "remediation proposed"])


@pytest.fixture
def make_mcp_tool() -> Callable[..., FakeMCPTool]:
    def _factory(name: str = "fake_mcp", *, failures: int = 0, result: Any = None) -> FakeMCPTool:
        effects: List[Any] = [RuntimeError(f"{name} transient") for _ in range(failures)]
        effects.append(result if result is not None else {"ok": True, "tool": name})
        return FakeMCPTool(name=name, side_effects=effects)

    return _factory


@pytest.fixture
def ws_ticket_for():
    def _ticket(tenant: Tenant) -> Dict[str, str]:
        return {
            "purpose": "ws",
            "org_id": tenant.org_id,
            "user_id": tenant.user_id,
        }

    return _ticket


@pytest.fixture
def org_scoped_incident():
    def _make(tenant: Tenant, **overrides: Any) -> SimpleNamespace:
        data = {
            "id": str(uuid.uuid4()),
            "org_id": tenant.org_id,
            "cluster_id": tenant.cluster_id,
            "title": f"incident-{tenant.name}",
            "status": "investigating",
        }
        data.update(overrides)
        return SimpleNamespace(**data)

    return _make
