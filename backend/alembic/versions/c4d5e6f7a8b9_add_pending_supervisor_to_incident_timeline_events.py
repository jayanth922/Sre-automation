"""Add pending supervisor fields to incident timeline events

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "incident_timeline_events",
        sa.Column("pending_supervisor", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "incident_timeline_events",
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "incident_timeline_events",
        "pending_supervisor",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("incident_timeline_events", "handled_at")
    op.drop_column("incident_timeline_events", "pending_supervisor")