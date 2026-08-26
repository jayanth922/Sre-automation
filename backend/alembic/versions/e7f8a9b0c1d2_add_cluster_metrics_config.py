"""Add per-cluster observability metrics_config

Revision ID: e7f8a9b0c1d2
Revises: c4d5e6f7a8b9
Create Date: 2026-07-29 00:00:00.000000

Stores each cluster's Prometheus query conventions (service label, request /
latency metric names, error selector) so the platform is not hardcoded to any
one workload's metric schema. Null = use the platform defaults.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column("metrics_config", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clusters", "metrics_config")
