"""Add truthful cluster heartbeat source/reason fields.

Revision ID: d5e6f7a8b9c0
Revises: c2d3e4f5a6b7
Create Date: 2026-08-28 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("heartbeat_source", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("heartbeat_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("clusters", "heartbeat_reason")
    op.drop_column("clusters", "heartbeat_source")
