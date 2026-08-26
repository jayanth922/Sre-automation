"""Add secure organization invitations and organization-scoped audit events.

Revision ID: 9c0d1e2f3a4b
Revises: 0a1b2c3d4e5f
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "9c0d1e2f3a4b"
down_revision: Union[str, None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column(
            "invited_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_org_invitations_organization_id",
        "org_invitations",
        ["organization_id"],
    )
    op.create_index("ix_org_invitations_email", "org_invitations", ["email"])
    op.create_index(
        "ix_org_invitations_token_hash",
        "org_invitations",
        ["token_hash"],
        unique=True,
    )

    op.add_column(
        "audit_events",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_events_organization_id_organizations",
        "audit_events",
        "organizations",
        ["organization_id"],
        ["id"],
    )
    op.create_index(
        "ix_audit_events_organization_id",
        "audit_events",
        ["organization_id"],
    )
    op.alter_column(
        "audit_events",
        "cluster_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_audit_events_has_scope",
        "audit_events",
        "cluster_id IS NOT NULL OR organization_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_audit_events_has_scope",
        "audit_events",
        type_="check",
    )
    # Organization-only audit records cannot fit the previous cluster-only
    # schema and must be removed before restoring the NOT NULL constraint.
    op.execute("DELETE FROM audit_events WHERE cluster_id IS NULL")
    op.alter_column(
        "audit_events",
        "cluster_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_index("ix_audit_events_organization_id", table_name="audit_events")
    op.drop_constraint(
        "fk_audit_events_organization_id_organizations",
        "audit_events",
        type_="foreignkey",
    )
    op.drop_column("audit_events", "organization_id")

    op.drop_index("ix_org_invitations_token_hash", table_name="org_invitations")
    op.drop_index("ix_org_invitations_email", table_name="org_invitations")
    op.drop_index(
        "ix_org_invitations_organization_id", table_name="org_invitations"
    )
    op.drop_table("org_invitations")
