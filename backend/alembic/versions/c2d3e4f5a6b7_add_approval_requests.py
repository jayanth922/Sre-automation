"""Add durable, single-use remediation approval requests.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("action_hash", sa.String(length=64), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("approver_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_requests_incident_status",
        "approval_requests",
        ["incident_id", "status"],
    )
    op.create_index(
        "ix_approval_requests_thread_id",
        "approval_requests",
        ["thread_id"],
    )
    op.create_index(
        "uq_approval_requests_pending_action",
        "approval_requests",
        ["thread_id", "action_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_approval_requests_pending_action", table_name="approval_requests"
    )
    op.drop_index("ix_approval_requests_thread_id", table_name="approval_requests")
    op.drop_index(
        "ix_approval_requests_incident_status", table_name="approval_requests"
    )
    op.drop_table("approval_requests")
