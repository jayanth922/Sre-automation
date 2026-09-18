"""Add durable content-addressed evidence artifacts.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_trace_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("stored_byte_count", sa.Integer(), nullable=False),
        sa.Column("content_encoding", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "incident_id",
            "kind",
            "source",
            "content_sha256",
            name="uq_evidence_artifact_content",
        ),
    )
    op.create_index(
        "ix_evidence_artifacts_incident_id",
        "evidence_artifacts",
        ["incident_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_root_trace_id",
        "evidence_artifacts",
        ["root_trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_incident_created",
        "evidence_artifacts",
        ["incident_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_artifacts_incident_created", table_name="evidence_artifacts"
    )
    op.drop_index(
        "ix_evidence_artifacts_root_trace_id", table_name="evidence_artifacts"
    )
    op.drop_index(
        "ix_evidence_artifacts_incident_id", table_name="evidence_artifacts"
    )
    op.drop_table("evidence_artifacts")
