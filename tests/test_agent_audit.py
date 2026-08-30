#!/usr/bin/env python3
"""Durable agent flight-recorder (AgentAuditLog) tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models import AgentAuditLog, Base, JobStatus
from sre_agent.agent_audit import (
    audit_retention_days,
    export_agent_audit_logs,
    purge_expired_agent_audit_logs,
)
from sre_agent.audit_context import (
    clear_audit_context,
    get_audit_context,
    note_audit_write_failure,
    pop_audit_write_failure,
    set_audit_context,
)


@pytest.fixture(autouse=True)
def _reset_audit_context():
    clear_audit_context()
    yield
    clear_audit_context()


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Avoid Postgres-only UUID defaults for sqlite unit tests.
    AgentAuditLog.__table__.create(bind=engine, checkfirst=True)
    session_local = sessionmaker(bind=engine)
    with session_local() as db:
        yield db


def test_agent_audit_log_is_on_canonical_base():
    assert AgentAuditLog.__table__.name == "agent_audit_logs"
    assert AgentAuditLog.__table__ in Base.metadata.tables.values()
    assert "organization_id" in AgentAuditLog.__table__.c
    assert "cluster_id" in AgentAuditLog.__table__.c
    assert "run_id" in AgentAuditLog.__table__.c


def test_audit_context_preserves_scope_when_updating_agent():
    set_audit_context(
        incident_id="inc-1",
        agent_name="Supervisor",
        organization_id="org-1",
        cluster_id="cluster-1",
        run_id="run-1",
    )
    set_audit_context(agent_name="KubernetesAgent")
    incident_id, agent_name, organization_id, cluster_id, run_id = get_audit_context()
    assert incident_id == "inc-1"
    assert agent_name == "KubernetesAgent"
    assert organization_id == "org-1"
    assert cluster_id == "cluster-1"
    assert run_id == "run-1"


def test_audit_write_failure_is_visible_for_job_health():
    assert pop_audit_write_failure() is None
    note_audit_write_failure("disk full")
    note_audit_write_failure("connection reset")
    assert pop_audit_write_failure() == "disk full; connection reset"
    assert pop_audit_write_failure() is None
    assert JobStatus.DEGRADED.value == "degraded"


def test_tool_audit_writer_persists_full_execution_scope(
    session: Session, monkeypatch
):
    from sre_agent import mcp_tool_wrapper, redis_state_store

    organization_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    set_audit_context(
        incident_id="inc-scoped",
        agent_name="KubernetesAgent",
        organization_id=str(organization_id),
        cluster_id=str(cluster_id),
        run_id="job-scoped",
    )
    monkeypatch.setattr(mcp_tool_wrapper, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        redis_state_store,
        "get_state_store",
        lambda: type("Store", (), {"append_log": lambda *args: None})(),
    )

    audit_id = mcp_tool_wrapper.log_audit_entry(
        "list_pods", "PENDING", {"namespace": "demo-app"}
    )
    row = session.get(AgentAuditLog, audit_id)

    assert row.organization_id == organization_id
    assert row.cluster_id == cluster_id
    assert row.incident_id == "inc-scoped"
    assert row.run_id == "job-scoped"


def test_flight_recorder_rows_are_queryable_by_tenant_and_run(session: Session):
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    cluster_a = uuid.uuid4()
    run_a = "job-a"
    session.add_all(
        [
            AgentAuditLog(
                id=uuid.uuid4(),
                timestamp=datetime.now(timezone.utc),
                organization_id=org_a,
                cluster_id=cluster_a,
                incident_id="inc-a",
                run_id=run_a,
                agent_name="KubernetesAgent",
                tool_name="get_pods",
                tool_args="{}",
                status="SUCCESS",
            ),
            AgentAuditLog(
                id=uuid.uuid4(),
                timestamp=datetime.now(timezone.utc),
                organization_id=org_b,
                cluster_id=uuid.uuid4(),
                incident_id="inc-b",
                run_id="job-b",
                agent_name="MetricsAgent",
                tool_name="query",
                tool_args="{}",
                status="SUCCESS",
            ),
        ]
    )
    session.commit()

    exported = export_agent_audit_logs(
        session, organization_id=org_a, cluster_id=cluster_a, run_id=run_a
    )
    assert len(exported) == 1
    assert exported[0]["tool_name"] == "get_pods"
    assert exported[0]["organization_id"] == str(org_a)
    assert exported[0]["run_id"] == run_a

    rows = (
        session.execute(
            select(AgentAuditLog).where(AgentAuditLog.incident_id == "inc-a")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_retention_purge_deletes_only_expired_rows(session: Session, monkeypatch):
    monkeypatch.setenv("AGENT_AUDIT_RETENTION_DAYS", "7")
    assert audit_retention_days() == 7
    now = datetime.now(timezone.utc)
    keep = AgentAuditLog(
        id=uuid.uuid4(),
        timestamp=now - timedelta(days=2),
        incident_id="keep",
        run_id="run",
        agent_name="A",
        tool_name="t",
        tool_args="{}",
        status="SUCCESS",
    )
    drop = AgentAuditLog(
        id=uuid.uuid4(),
        timestamp=now - timedelta(days=30),
        incident_id="drop",
        run_id="run",
        agent_name="A",
        tool_name="t",
        tool_args="{}",
        status="SUCCESS",
    )
    session.add_all([keep, drop])
    session.commit()

    deleted = purge_expired_agent_audit_logs(session, now=now)
    assert deleted == 1
    remaining = session.execute(select(AgentAuditLog)).scalars().all()
    assert [row.incident_id for row in remaining] == ["keep"]


def test_migration_targets_canonical_alembic_chain():
    from pathlib import Path

    migration = Path(
        "backend/alembic/versions/d3ac85ffcc7d_add_agent_audit_logs.py"
    ).read_text()
    assert (
        'create_table(\n        "agent_audit_logs"' in migration
        or 'create_table(\n        "agent_audit_logs",' in migration
    )
    assert "agent_audit_logs" in migration
    assert "down_revision" in migration
    assert "b1c7ceb2036b" in migration
    assert "AgentAuditLog" in Path("backend/models.py").read_text()
    assert (
        "class AgentAuditLog"
        not in Path("sre_agent/models.py")
        .read_text()
        .split("Compatibility re-export")[0]
    )
