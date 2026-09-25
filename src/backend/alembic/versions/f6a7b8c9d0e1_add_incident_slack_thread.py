"""Add slack_channel/slack_thread_ts to incidents

Revision ID: f6a7b8c9d0e1
Revises: 2253eabf13e3
Create Date: 2026-08-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "2253eabf13e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("slack_channel", sa.String(length=64), nullable=True))
    op.add_column("incidents", sa.Column("slack_thread_ts", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "slack_thread_ts")
    op.drop_column("incidents", "slack_channel")
