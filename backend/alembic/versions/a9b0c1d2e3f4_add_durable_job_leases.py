"""Alembic migration for R02 durable job lease fields.

Revision ID: a9b0c1d2e3f4
Revises: f1a2b3c4d5e6
Create Date: 2026-08-28 17:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("incident_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column("jobs", sa.Column("lease_owner", sa.String(length=200), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("jobs", sa.Column("last_error", sa.Text(), nullable=True))

    op.create_foreign_key(
        "fk_jobs_organization_id",
        "jobs",
        "organizations",
        ["organization_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_jobs_incident_id",
        "jobs",
        "incidents",
        ["incident_id"],
        ["id"],
    )
    op.create_index("ix_jobs_organization_id", "jobs", ["organization_id"])
    op.create_index("ix_jobs_incident_id", "jobs", ["incident_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_lease_expires_at", "jobs", ["lease_expires_at"])
    op.create_unique_constraint(
        "uq_jobs_cluster_idempotency_key",
        "jobs",
        ["cluster_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_jobs_cluster_idempotency_key", "jobs", type_="unique")
    op.drop_index("ix_jobs_lease_expires_at", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_incident_id", table_name="jobs")
    op.drop_index("ix_jobs_organization_id", table_name="jobs")
    op.drop_constraint("fk_jobs_incident_id", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_organization_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "last_error")
    op.drop_column("jobs", "cancel_requested_at")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "lease_owner")
    op.drop_column("jobs", "max_attempts")
    op.drop_column("jobs", "attempt_count")
    op.drop_column("jobs", "idempotency_key")
    op.drop_column("jobs", "incident_id")
    op.drop_column("jobs", "organization_id")
