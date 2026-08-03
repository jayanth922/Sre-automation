"""Add per-cluster scope (namespace) and LLM override

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-03 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("namespace", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("llm_provider", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("llm_model", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("llm_base_url", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("llm_api_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("clusters", "llm_api_key")
    op.drop_column("clusters", "llm_base_url")
    op.drop_column("clusters", "llm_model")
    op.drop_column("clusters", "llm_provider")
    op.drop_column("clusters", "namespace")
