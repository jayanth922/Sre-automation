"""Add agent_audit_logs on the canonical backend.models Base (P07).

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_audit_logs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_args", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_audit_logs_incident_id",
        "agent_audit_logs",
        ["incident_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_audit_logs_incident_id", table_name="agent_audit_logs")
    op.drop_table("agent_audit_logs")
