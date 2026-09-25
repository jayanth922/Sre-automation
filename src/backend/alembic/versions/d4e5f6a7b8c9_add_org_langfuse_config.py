"""Add per-organization Langfuse config

Revision ID: d4e5f6a7b8c9
Revises: c9d0e1f2a3b4
Create Date: 2026-09-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("langfuse_public_key", sa.String(), nullable=True))
    op.add_column("organizations", sa.Column("langfuse_secret_key", sa.String(), nullable=True))
    op.add_column("organizations", sa.Column("langfuse_host", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "langfuse_host")
    op.drop_column("organizations", "langfuse_secret_key")
    op.drop_column("organizations", "langfuse_public_key")
