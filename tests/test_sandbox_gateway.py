"""Tests for the sole authorization boundary for sandbox Job provisioning."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sre_agent.execution_context import ExecutionContext  # noqa: E402
from sre_agent.sandbox_gateway import (  # noqa: E402
    SandboxAuditError,
    SandboxGateContext,
    SandboxRejected,
    authorize_and_provision_sandbox,
)

CONTEXT = ExecutionContext(
    organization_id="11111111-1111-1111-1111-111111111111",
    cluster_id="22222222-2222-2222-2222-222222222222",
    namespace="demo-app",
    allowlist=("demo-app",),
)

GATE_CONTEXT = SandboxGateContext(
    incident_id="incident-1",
    organization_id="11111111-1111-1111-1111-111111111111",
    cluster_id="22222222-2222-2222-2222-222222222222",
)


class FakeStore:
    def __init__(self, *, available: bool = True):
        self._available = available
        self.claims = set()

    def is_available(self):
        return self._available

    def set_idempotency(self, key, ttl):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


def _patch_audit(monkeypatch, captured=None):
    async def fake_persist(gate_context, tool_name, job_name, status, detail):
        if captured is not None:
            captured.append((tool_name, job_name, status, detail))

    monkeypatch.setattr("sre_agent.sandbox_gateway._persist_audit_event", fake_persist)


def test_scope_mismatch_is_rejected_before_any_call(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    other_gate = SandboxGateContext(
        incident_id="incident-1",
        organization_id="99999999-9999-9999-9999-999999999999",
        cluster_id="22222222-2222-2222-2222-222222222222",
    )
    with pytest.raises(SandboxRejected, match="scope_mismatch"):
        asyncio.run(
            authorize_and_provision_sandbox(
                other_gate, CONTEXT, caller, "provision", {}, "key-1"
            )
        )
    assert calls == []
    assert store.claims == set()


def test_empty_idempotency_key_is_rejected(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    with pytest.raises(SandboxRejected, match="invalid_idempotency_key"):
        asyncio.run(
            authorize_and_provision_sandbox(
                GATE_CONTEXT, CONTEXT, caller, "provision", {}, "   "
            )
        )
    assert calls == []


def test_state_store_unavailable_is_rejected(monkeypatch):
    store = FakeStore(available=False)
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))

    with pytest.raises(SandboxRejected, match="state_unavailable"):
        asyncio.run(
            authorize_and_provision_sandbox(
                GATE_CONTEXT, CONTEXT, caller, "provision", {}, "key-1"
            )
        )
    assert calls == []


def test_duplicate_idempotency_key_short_circuits_without_calling_tool(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    _patch_audit(monkeypatch)
    calls = []

    async def caller(tool, args):
        calls.append((tool, args))
        return json.dumps({"status": "OK", "job_name": "sbx-1"})

    first = asyncio.run(
        authorize_and_provision_sandbox(
            GATE_CONTEXT, CONTEXT, caller, "provision", {"job_name": "sbx-1"}, "same-key"
        )
    )
    assert first["status"] == "OK"

    duplicate = asyncio.run(
        authorize_and_provision_sandbox(
            GATE_CONTEXT, CONTEXT, caller, "provision", {"job_name": "sbx-1"}, "same-key"
        )
    )
    assert duplicate["status"] == "SKIPPED"
    assert len(calls) == 1


def test_namespace_is_forced_onto_arguments(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    monkeypatch.setenv("SANDBOX_NAMESPACE", "sentinel-sandbox")
    _patch_audit(monkeypatch)
    seen = {}

    async def caller(tool, args):
        seen.update(args)
        return {"status": "OK"}

    asyncio.run(
        authorize_and_provision_sandbox(
            GATE_CONTEXT,
            CONTEXT,
            caller,
            "provision",
            {"namespace": "attacker-supplied"},
            "key-2",
        )
    )
    assert seen["namespace"] == "sentinel-sandbox"


def test_tool_error_persists_audit_and_reraises(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)
    captured = []
    _patch_audit(monkeypatch, captured)

    async def caller(tool, args):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            authorize_and_provision_sandbox(
                GATE_CONTEXT, CONTEXT, caller, "provision", {}, "key-3"
            )
        )
    assert captured and captured[0][2] == "ERROR"


def test_audit_persistence_failure_raises_sandbox_audit_error(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)

    async def failing_persist(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("sre_agent.sandbox_gateway._persist_audit_event", failing_persist)

    async def caller(tool, args):
        return {"status": "OK"}

    with pytest.raises(SandboxAuditError):
        asyncio.run(
            authorize_and_provision_sandbox(
                GATE_CONTEXT, CONTEXT, caller, "provision", {}, "key-4"
            )
        )


def test_audit_event_records_organization_and_cluster(monkeypatch):
    from backend import models

    store = FakeStore()
    monkeypatch.setattr("sre_agent.sandbox_gateway.get_state_store", lambda: store)

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

    async def caller(tool, args):
        return {"status": "OK", "job_name": "sbx-5"}

    asyncio.run(
        authorize_and_provision_sandbox(
            GATE_CONTEXT, CONTEXT, caller, "provision", {"job_name": "sbx-5"}, "key-5"
        )
    )
    assert len(captured) == 1
    event = captured[0]
    assert isinstance(event, models.AuditEvent)
    assert str(event.organization_id) == GATE_CONTEXT.organization_id
    assert str(event.cluster_id) == GATE_CONTEXT.cluster_id
    assert event.action_type == "SANDBOX_PROVISION"
