"""Add multi-tenant secure access fields (GitHub App installation, Slack OAuth).

Revision ID: a3f7c1d9b2e4
Revises: db94419c24dc
Create Date: 2026-08-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f7c1d9b2e4"
down_revision: Union[str, None] = "db94419c24dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column("github_app_installation_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organizations", sa.Column("slack_bot_token", sa.Text(), nullable=True)
    )
    op.add_column(
        "organizations", sa.Column("slack_team_id", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("organizations", "slack_team_id")
    op.drop_column("organizations", "slack_bot_token")
    op.drop_column("clusters", "github_app_installation_id")
