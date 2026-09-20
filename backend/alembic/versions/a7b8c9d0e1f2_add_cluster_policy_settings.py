"""Add per-cluster environment label and approval window.

Both columns are nullable, and null is meaningful: it means "this cluster has
no opinion, use the deployment default". Backfilling them from the operator's
current env vars would destroy that distinction -- every existing cluster would
end up pinned to whatever SENTINEL_CLUSTER_ENVIRONMENT happened to say on
migration day, and would stop following the default afterwards.

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-09-20 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("environment", sa.String(), nullable=True))
    op.add_column(
        "clusters", sa.Column("approval_ttl_minutes", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("clusters", "approval_ttl_minutes")
    op.drop_column("clusters", "environment")
