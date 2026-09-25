"""Add cluster infra fields, SLO table, audit_events table

Revision ID: a1b2c3d4e5f6
Revises: d6d22479d2ee
Create Date: 2026-03-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d6d22479d2ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Cluster: add infrastructure connectivity fields --
    op.add_column("clusters", sa.Column("prometheus_url", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("loki_url", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("k8s_api_server", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("k8s_token", sa.Text(), nullable=True))
    op.add_column("clusters", sa.Column("github_token", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("github_repo", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("notion_api_key", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("notion_database_id", sa.String(), nullable=True))

    # -- AuditEvent table (SOC2 compliance trail) --
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("actor_type", sa.String(), default="AGENT"),
        sa.Column("actor_id", sa.String()),
        sa.Column("action_type", sa.String()),
        sa.Column("resource_target", sa.String()),
        sa.Column("outcome", sa.String()),
        sa.Column("details", sa.Text(), nullable=True),
    )

    # -- SLO table --
    op.create_table(
        "slos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sli_metric", sa.String(200), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("window_days", sa.Integer(), default=30),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("error_budget_remaining", sa.Float(), nullable=True),
        sa.Column("last_calculated", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("slos")
    op.drop_table("audit_events")
    op.drop_column("clusters", "notion_database_id")
    op.drop_column("clusters", "notion_api_key")
    op.drop_column("clusters", "github_repo")
    op.drop_column("clusters", "github_token")
    op.drop_column("clusters", "k8s_token")
    op.drop_column("clusters", "k8s_api_server")
    op.drop_column("clusters", "loki_url")
    op.drop_column("clusters", "prometheus_url")
