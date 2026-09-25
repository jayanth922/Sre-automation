"""Add remediation_gate_approvals for Phase 5's two Temporal approval gates.

Revision ID: e4f5a6b7c8d9
Revises: a3f7c1d9b2e4
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "a3f7c1d9b2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "remediation_gate_approvals",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("gate", sa.String(length=20), nullable=False),
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
        "ix_remediation_gate_approvals_incident_status",
        "remediation_gate_approvals",
        ["incident_id", "status"],
    )
    op.create_index(
        "ix_remediation_gate_approvals_workflow_id",
        "remediation_gate_approvals",
        ["workflow_id"],
    )
    op.create_index(
        "uq_remediation_gate_approvals_pending_gate",
        "remediation_gate_approvals",
        ["workflow_id", "gate"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_remediation_gate_approvals_pending_gate", table_name="remediation_gate_approvals"
    )
    op.drop_index(
        "ix_remediation_gate_approvals_workflow_id", table_name="remediation_gate_approvals"
    )
    op.drop_index(
        "ix_remediation_gate_approvals_incident_status", table_name="remediation_gate_approvals"
    )
    op.drop_table("remediation_gate_approvals")
