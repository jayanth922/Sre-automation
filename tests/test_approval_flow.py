#!/usr/bin/env python3
"""T07 approval hashing, expiry, restart, and source-contract coverage."""

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "sre_agent" / "approval_flow.py"
spec = importlib.util.spec_from_file_location("approval_flow_under_test", MODULE_PATH)
approval_flow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = approval_flow
spec.loader.exec_module(approval_flow)


def test_action_hash_is_canonical_and_detects_tampering():
    first = {"actions": [{"target": "api", "replicas": 2}], "severity": "SEV2"}
    reordered = {"severity": "SEV2", "actions": [{"replicas": 2, "target": "api"}]}
    tampered = {"severity": "SEV2", "actions": [{"replicas": 20, "target": "api"}]}

    assert approval_flow.compute_action_hash(first) == approval_flow.compute_action_hash(reordered)
    assert approval_flow.compute_action_hash(first) != approval_flow.compute_action_hash(tampered)


def test_action_hash_ignores_only_volatile_dry_run_audit_hash():
    first = {
        "action_reports": [
            {"action_type": "scale", "parameters": {"replicas": 2}, "audit_hash": "old"}
        ]
    }
    rerun = {
        "action_reports": [
            {"action_type": "scale", "parameters": {"replicas": 2}, "audit_hash": "new"}
        ]
    }
    tampered = {
        "action_reports": [
            {"action_type": "scale", "parameters": {"replicas": 20}, "audit_hash": "new"}
        ]
    }
    assert approval_flow.compute_action_hash(first) == approval_flow.compute_action_hash(rerun)
    assert approval_flow.compute_action_hash(first) != approval_flow.compute_action_hash(tampered)


def test_expiry_is_timezone_safe_and_boundary_is_expired():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert approval_flow.is_expired(now, now)
    assert approval_flow.is_expired(now - timedelta(seconds=1), now)
    assert not approval_flow.is_expired(now + timedelta(seconds=1), now)
    assert approval_flow.is_expired(now.replace(tzinfo=None), now)


def test_tampered_hash_is_rejected():
    with pytest.raises(approval_flow.ApprovalValidationError, match="hash_mismatch"):
        approval_flow.validate_pending_approval(
            status="pending",
            stored_action_hash="a" * 64,
            submitted_action_hash="b" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )


def test_expired_approval_is_rejected():
    now = datetime.now(timezone.utc)
    with pytest.raises(approval_flow.ApprovalValidationError, match="expired"):
        approval_flow.validate_pending_approval(
            status="pending",
            stored_action_hash="a" * 64,
            submitted_action_hash="a" * 64,
            expires_at=now - timedelta(seconds=1),
            now=now,
        )


def test_decided_approval_replay_is_rejected():
    with pytest.raises(approval_flow.ApprovalValidationError, match="not_pending"):
        approval_flow.validate_pending_approval(
            status="approved",
            stored_action_hash="a" * 64,
            submitted_action_hash="a" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )


def test_interrupt_payload_binds_request_thread_report_and_hash():
    expires = datetime(2026, 8, 25, tzinfo=timezone.utc)
    pending = approval_flow.PendingApproval(
        id="request-1",
        incident_id="incident-1",
        thread_id="thread-1",
        action_hash="a" * 64,
        expires_at=expires,
    )
    report = {"aggregate_decision": "requires_approval"}
    payload = pending.interrupt_payload(report)

    assert payload == {
        "type": "approval_required",
        "approval_request_id": "request-1",
        "incident_id": "incident-1",
        "thread_id": "thread-1",
        "report": report,
        "action_hash": "a" * 64,
        "expires_at": expires.isoformat(),
    }


# ── the ask a human actually reads ───────────────────────────────────────────
# Slack is the only channel, so a persisted approval nobody is told about is an
# approval that expires in silence. These pin what the message must carry.

APPROVAL_REPORT = {
    "severity": "UNKNOWN",
    "aggregate_decision": "requires_approval",
    "confidence_status": "uncalibrated",
    "raw_action_confidence": 0.48,
    "action_reports": [
        {
            "action_type": "inspect",
            "target": "deployment/inventory-service",
            "namespace": "meridian",
            "decision": "autonomous",
            "reversibility": "read_only",
            "reason": "read-only: mutates nothing",
        },
        {
            "action_type": "config_change",
            "target": "inventory-service",
            "namespace": "meridian",
            "decision": "requires_approval",
            "reversibility": "risky",
            "reason": "uncalibrated self-confidence cannot authorize a mutation",
        },
        {
            "action_type": "patch",
            "target": "payments",
            "namespace": "kube-system",
            "decision": "blocked",
            "reversibility": "risky",
            "reason": "blocked: targets namespace 'kube-system', outside this cluster's scope",
        },
    ],
}


def test_approval_request_message_states_what_would_run_and_how_to_authorize():
    expires = datetime(2026, 9, 13, 19, 14, 42, tzinfo=timezone.utc)
    text = approval_flow.format_approval_request(APPROVAL_REPORT, expires)

    # Severity, gate, and the honest confidence label.
    assert "UNKNOWN" in text
    assert "requires_approval" in text
    assert "uncalibrated" in text
    assert "0.48" in text
    # Every action, its target and its own decision — not just a count.
    for fragment in (
        "inspect",
        "config_change",
        "inventory-service",
        "patch",
        "kube-system",
        "blocked",
        "read_only",
        "risky",
    ):
        assert fragment in text, fragment
    # Only the held action is what approving would run.
    assert "runs 1 held action" in text
    # The exact words that authorize it, and the deadline.
    assert f"`{approval_flow.APPROVAL_COMMAND}`" in text
    assert "2026-09-13 19:14:42Z" in text


def test_approval_request_message_fits_the_slack_forwarder_budget():
    """`war_room.format_event_for_slack` truncates content at 1500 chars; the
    ask must not lose its own instructions to a long plan."""
    report = {
        **APPROVAL_REPORT,
        "action_reports": APPROVAL_REPORT["action_reports"] * 12,
    }
    text = approval_flow.format_approval_request(report, datetime.now(timezone.utc))

    assert len(text) <= 1500
    assert "and 28 more" in text
    assert f"`{approval_flow.APPROVAL_COMMAND}`" in text


def test_approval_request_survives_a_naive_expiry_and_missing_confidence():
    text = approval_flow.format_approval_request(
        {"action_reports": []}, datetime(2026, 9, 13, 19, 14, 42)
    )
    assert "UNKNOWN" in text
    assert "uncalibrated" in text
    assert "2026-09-13 19:14:42Z" in text


def test_pending_approval_defaults_to_created_so_reuse_must_be_explicit():
    pending = approval_flow.PendingApproval(
        id="request-1",
        incident_id="incident-1",
        thread_id="thread-1",
        action_hash="a" * 64,
        expires_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    assert pending.created is True
    assert "created" not in pending.interrupt_payload({})


def test_current_interrupt_reads_langgraph_style_snapshot():
    wanted = {"type": "approval_required", "action_hash": "b" * 64}
    snapshot = SimpleNamespace(
        tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value=wanted),)),)
    )
    assert approval_flow.current_approval_interrupt(snapshot) is wanted


def test_runtime_restart_resumes_same_checkpointed_thread(monkeypatch):
    """Drop the compiled graph and reconstruct it through get_checkpointer()."""
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, StateGraph
    from langgraph.types import Command, interrupt

    checkpointer_path = ROOT / "sre_agent" / "checkpointer.py"
    cp_spec = importlib.util.spec_from_file_location("checkpointer_for_approval_test", checkpointer_path)
    checkpointer = importlib.util.module_from_spec(cp_spec)
    sys.modules[cp_spec.name] = checkpointer
    cp_spec.loader.exec_module(checkpointer)

    class State(TypedDict, total=False):
        action_hash: str
        approved: bool

    saver = MemorySaver()
    monkeypatch.setenv("CHECKPOINTER_ENABLED", "true")
    monkeypatch.setattr(checkpointer, "_memory_saver", lambda: saver)

    async def build_graph():
        workflow = StateGraph(State)

        def gate(state):
            resumed = interrupt(
                {"type": "approval_required", "action_hash": state["action_hash"]}
            )
            return {"approved": resumed["action_hash"] == state["action_hash"]}

        workflow.add_node("gate", gate)
        workflow.set_entry_point("gate")
        workflow.add_edge("gate", END)
        return workflow.compile(checkpointer=await checkpointer.get_checkpointer())

    async def scenario():
        config = {"configurable": {"thread_id": "incident-1"}}
        graph = await build_graph()
        await graph.ainvoke({"action_hash": "c" * 64}, config=config)
        assert approval_flow.current_approval_interrupt(await graph.aget_state(config))

        del graph
        reconstructed = await build_graph()
        output = await reconstructed.ainvoke(
            Command(resume={"action_hash": "c" * 64}), config=config
        )
        assert output["approved"] is True

    asyncio.run(scenario())


def test_graph_and_api_enforce_verified_synchronous_resume():
    graph_source = (ROOT / "sre_agent" / "graph_builder.py").read_text()
    api_source = (ROOT / "sre_agent" / "api" / "v1" / "mission_control.py").read_text()

    assert "interrupt(pending)" in graph_source
    assert 'workflow.add_edge("aggregate", "approval_prepare")' in graph_source
    assert 'workflow.add_edge("approval_gate", "act_gate")' in graph_source
    assert "compute_action_hash(interrupt_report)" in api_source
    assert "if not durable_checkpointer_configured():" in api_source
    assert '"approval": interrupt_payload' in api_source
    assert "models.ApprovalRequest.status == models.ApprovalStatus.PENDING" in api_source
    assert "models.ApprovalRequest.expires_at > now" in api_source
    assert "cas.rowcount != 1" in api_source
    assert "await graph.ainvoke(" in api_source
    assert "asyncio.create_task(\n            graph.ainvoke(Command(resume" not in api_source

    dashboard_source = (
        ROOT
        / "dashboard"
        / "app"
        / "(dashboard)"
        / "clusters"
        / "[id]"
        / "incidents"
        / "[incidentId]"
        / "page.tsx"
    ).read_text()
    # Slack is the only approval channel by design, so the dashboard's former
    # "Approve & run" button is gone: the incident page states the pending plan
    # and points at the thread. It must not grow a second way to authorize a
    # mutation — one channel is what makes the audit trail complete.
    assert 'Reply "approve fix" in the incident\'s Slack thread' in dashboard_source
    assert "/approve" not in dashboard_source

    # And the ask has to reach that thread: the graph's interrupt is silent, so
    # approval_prepare emits it and the war room forwards that event type.
    war_room_source = (ROOT / "sre_agent" / "war_room.py").read_text()
    assert 'event_type="approval"' in graph_source
    assert '"approval"' in war_room_source.split("_SURFACED = ")[1].split("\n")[0]


def test_async_postgres_checkpointer_is_configured_for_api_restart():
    source = (ROOT / "sre_agent" / "checkpointer.py").read_text()
    runtime_source = (ROOT / "sre_agent" / "agent_runtime.py").read_text()
    dependencies = (ROOT / "pyproject.toml").read_text()
    helm_values = (ROOT / "deploy" / "helm" / "sentinel" / "values.yaml").read_text()

    assert "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver" in source
    assert "await saver.setup()" in source
    assert 'os.getenv("DATABASE_URL")' in source
    assert 'raise RuntimeError("Configured durable checkpointer is unavailable")' in source
    assert "checkpointer = await get_checkpointer()" in runtime_source
    assert '"langgraph-checkpoint-postgres>=2.0.0"' in dependencies
    assert 'checkpointerBackend: "postgres"' in helm_values


def test_model_and_migration_include_all_durable_approval_fields():
    model_source = (ROOT / "backend" / "models.py").read_text()
    migration_source = (
        ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "c2d3e4f5a6b7_add_approval_requests.py"
    ).read_text()
    for field in (
        "incident_id",
        "thread_id",
        "action_hash",
        "organization_id",
        "cluster_id",
        "status",
        "approver_user_id",
        "decided_at",
        "expires_at",
        "created_at",
    ):
        assert field in model_source
        assert f'"{field}"' in migration_source

    flow_source = (ROOT / "sre_agent" / "approval_flow.py").read_text()
    assert ".values(status=models.IncidentStatus.AWAITING_APPROVAL)" in flow_source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
