"""Add immutable incident run manifests.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-26 00:00:00.000000
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
        "run_manifests",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("comparable", sa.Boolean(), nullable=False),
        sa.Column("non_comparable_reasons", sa.JSON(), nullable=False),
        sa.Column("root_trace_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_run_manifests_incident_created",
        "run_manifests",
        ["incident_id", "created_at"],
    )
    op.create_index(
        "ix_run_manifests_tenant_created",
        "run_manifests",
        ["organization_id", "cluster_id", "created_at"],
    )
    op.create_index(
        "ix_run_manifests_root_trace_id",
        "run_manifests",
        ["root_trace_id"],
    )
    op.execute("""
        CREATE FUNCTION reject_run_manifest_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'run manifests are immutable';
        END;
        $$ LANGUAGE plpgsql
        """)
    op.execute("""
        CREATE TRIGGER run_manifests_reject_update
        BEFORE UPDATE ON run_manifests
        FOR EACH ROW EXECUTE FUNCTION reject_run_manifest_update()
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS run_manifests_reject_update ON run_manifests")
    op.execute("DROP FUNCTION IF EXISTS reject_run_manifest_update()")
    op.drop_index("ix_run_manifests_root_trace_id", table_name="run_manifests")
    op.drop_index("ix_run_manifests_tenant_created", table_name="run_manifests")
    op.drop_index("ix_run_manifests_incident_created", table_name="run_manifests")
    op.drop_table("run_manifests")
