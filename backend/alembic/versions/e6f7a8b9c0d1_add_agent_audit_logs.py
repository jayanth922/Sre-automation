"""Add migrated agent_audit_logs flight-recorder table.

Revision ID: e6f7a8b9c0d1
Revises: c2d3e4f5a6b7
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_audit_logs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("incident_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_args", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
    )
    op.create_index("ix_agent_audit_logs_timestamp", "agent_audit_logs", ["timestamp"])
    op.create_index(
        "ix_agent_audit_logs_organization_id", "agent_audit_logs", ["organization_id"]
    )
    op.create_index(
        "ix_agent_audit_logs_cluster_id", "agent_audit_logs", ["cluster_id"]
    )
    op.create_index(
        "ix_agent_audit_logs_incident_id", "agent_audit_logs", ["incident_id"]
    )
    op.create_index("ix_agent_audit_logs_run_id", "agent_audit_logs", ["run_id"])
    op.create_index(
        "ix_agent_audit_logs_org_timestamp",
        "agent_audit_logs",
        ["organization_id", "timestamp"],
    )
    op.create_index(
        "ix_agent_audit_logs_cluster_timestamp",
        "agent_audit_logs",
        ["cluster_id", "timestamp"],
    )
    op.create_index(
        "ix_agent_audit_logs_incident_timestamp",
        "agent_audit_logs",
        ["incident_id", "timestamp"],
    )
    op.create_index(
        "ix_agent_audit_logs_run_timestamp",
        "agent_audit_logs",
        ["run_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_audit_logs_run_timestamp", table_name="agent_audit_logs")
    op.drop_index(
        "ix_agent_audit_logs_incident_timestamp", table_name="agent_audit_logs"
    )
    op.drop_index(
        "ix_agent_audit_logs_cluster_timestamp", table_name="agent_audit_logs"
    )
    op.drop_index("ix_agent_audit_logs_org_timestamp", table_name="agent_audit_logs")
    op.drop_index("ix_agent_audit_logs_run_id", table_name="agent_audit_logs")
    op.drop_index("ix_agent_audit_logs_incident_id", table_name="agent_audit_logs")
    op.drop_index("ix_agent_audit_logs_cluster_id", table_name="agent_audit_logs")
    op.drop_index("ix_agent_audit_logs_organization_id", table_name="agent_audit_logs")
    op.drop_index("ix_agent_audit_logs_timestamp", table_name="agent_audit_logs")
    op.drop_table("agent_audit_logs")
