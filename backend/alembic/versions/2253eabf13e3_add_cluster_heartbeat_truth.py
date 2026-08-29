"""Add truthful cluster heartbeat source/reason fields.

Revision ID: 2253eabf13e3
Revises: d3ac85ffcc7d
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2253eabf13e3"
down_revision: Union[str, None] = "d3ac85ffcc7d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("heartbeat_source", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("heartbeat_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("clusters", "heartbeat_reason")
    op.drop_column("clusters", "heartbeat_source")
