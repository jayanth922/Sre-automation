"""Guardrails for the sole live-mutation entrypoint."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from sre_agent.execution_context import ExecutionContext
from sre_agent.mutation_gateway import (
    MutationGateContext,
    MutationRejected,
    _persist_audit_event,
    authorize_and_execute,
)
from sre_agent.policy_gate import (
    AutonomyDecision,
    GateDecision,
    Reversibility,
)
from sre_agent.severity_engine import Severity


@dataclass
class FakeAction:
    action_type: str = "restart"
    target: str = "checkout-service"
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {"namespace": "demo-app"}
    )
    rollback_plan: str = "roll back"


class FakeStore:
    def __init__(self, *, locked: bool = False):
        self.locked = locked
        self.claims = set()
        self.lock_reads = 0

    def is_available(self):
        return True

    def is_cluster_locked(self, cluster_id):
        self.lock_reads += 1
        return self.locked

    def set_idempotency(self, key, ttl):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


CONTEXT = ExecutionContext(
    organization_id="11111111-1111-1111-1111-111111111111",
    cluster_id="22222222-2222-2222-2222-222222222222",
    namespace="demo-app",
    allowlist=("demo-app",),
)


def _planned(decision=AutonomyDecision.AUTONOMOUS):
    return GateDecision(
        decision=decision,
        severity=Severity.SEV3,
        reversibility=Reversibility.REVERSIBLE,
        allowed_by_policy=True,
        reason="planned decision",
    )


def _fresh(decision=AutonomyDecision.AUTONOMOUS):
    return GateDecision(
        decision=decision,
        severity=Severity.SEV3,
        reversibility=Reversibility.REVERSIBLE,
        allowed_by_policy=decision is not AutonomyDecision.BLOCKED,
        reason="fresh decision",
    )


async def _no_audit(*args, **kwargs):
    return None


def test_lock_between_plan_and_execute_hard_blocks(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    store = FakeStore(locked=True)
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "_persist_audit_event", _no_audit)

    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    with pytest.raises(MutationRejected, match="cluster_locked"):
        asyncio.run(
            authorize_and_execute(
                FakeAction(), _planned(), CONTEXT, caller, None, "lock-race"
            )
        )
    assert store.lock_reads == 1
    assert calls == []


def test_policy_mutation_between_plan_and_execute_hard_blocks(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    store = FakeStore()
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "decide", lambda *args, **kwargs: _fresh(AutonomyDecision.BLOCKED))
    monkeypatch.setattr(gateway, "_persist_audit_event", _no_audit)

    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    with pytest.raises(MutationRejected, match="policy_blocked"):
        asyncio.run(
            authorize_and_execute(
                FakeAction(), _planned(), CONTEXT, caller, None, "policy-race"
            )
        )
    assert calls == []
    assert store.claims == set()


def test_fresh_policy_ignores_alert_or_stale_gate_environment(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    store = FakeStore()
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    stale = MutationGateContext(
        decision="autonomous",
        severity=Severity.SEV3,
        environment="development",
        risk_score=5.0,
    )
    with pytest.raises(MutationRejected, match="policy_blocked"):
        asyncio.run(
            authorize_and_execute(
                FakeAction(), stale, CONTEXT, caller, None, "spoofed-environment"
            )
        )
    assert calls == []
    assert store.claims == set()


def test_idempotency_short_circuits_second_mutation(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    store = FakeStore()
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "decide", lambda *args, **kwargs: _fresh())

    audits = []

    async def persist(context, result, *, approved):
        audits.append(result.audit)

    monkeypatch.setattr(gateway, "_persist_audit_event", persist)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))
        return {"status": "OK", "applied": True}

    first = asyncio.run(
        authorize_and_execute(
            FakeAction(), _planned(), CONTEXT, caller, None, "same-action"
        )
    )
    assert first.status == "EXECUTED"
    assert len(first.audit["content_hash"]) == 64

    duplicate = asyncio.run(
        authorize_and_execute(
            FakeAction(), _planned(), CONTEXT, caller, None, "same-action"
        )
    )
    assert duplicate.status == "SKIPPED"
    assert "idempotency" in duplicate.detail
    assert len(calls) == 1
    assert len(audits) == 1


def test_namespace_mismatch_blocks_before_idempotency_or_tool_call(monkeypatch):
    import sre_agent.mutation_gateway as gateway

    store = FakeStore()
    monkeypatch.setattr(gateway, "get_state_store", lambda: store)
    monkeypatch.setattr(gateway, "decide", lambda *args, **kwargs: _fresh())
    action = FakeAction(parameters={"namespace": "other-tenant"})

    with pytest.raises(MutationRejected, match="scope_mismatch"):
        asyncio.run(
            authorize_and_execute(
                action,
                _planned(),
                CONTEXT,
                lambda *args: None,
                None,
                "wrong-scope",
            )
        )
    assert store.claims == set()


def test_redis_idempotency_claim_uses_one_set_nx_ex(monkeypatch):
    from sre_agent.redis_state_store import RedisStateStore

    calls = []

    class Client:
        def set(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    store = object.__new__(RedisStateStore)
    store.redis_client = Client()
    monkeypatch.setattr(store, "is_available", lambda: True)

    assert store.set_idempotency("mutation-1", 60) is True
    assert calls == [
        (
            ("sre_agent:idempotency:mutation-1", "CLAIMED"),
            {"nx": True, "ex": 60},
        )
    ]


def test_audit_event_persists_executor_content_hash(monkeypatch):
    from backend import models
    from sre_agent.executor import Executor

    captured = []

    class Session:
        def add(self, event):
            captured.append(event)

        async def commit(self):
            return None

    @asynccontextmanager
    async def session_local():
        yield Session()

    fake_database = SimpleNamespace(AsyncSessionLocal=session_local)
    fake_backend = __import__("backend")
    monkeypatch.setattr(fake_backend, "database", fake_database, raising=False)
    monkeypatch.setattr(fake_backend, "models", models, raising=False)
    monkeypatch.setitem(sys.modules, "backend.database", fake_database)

    result = Executor(actor="sre-agent", incident_id="incident-1").execute(
        FakeAction(), "autonomous", dry_run=True
    )
    result.status = "EXECUTED"
    asyncio.run(_persist_audit_event(CONTEXT, result, approved=False))

    assert isinstance(captured[0], models.AuditEvent)
    assert json.loads(captured[0].details)["content_hash"] == result.audit["content_hash"]


def test_live_executor_has_no_direct_application_bypass():
    source_root = Path(__file__).resolve().parents[1] / "sre_agent"
    offenders = []
    for path in source_root.rglob("*.py"):
        if path.name == "mutation_gateway.py":
            continue
        if "._aexecute_unchecked(" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(source_root)))
    assert offenders == []
