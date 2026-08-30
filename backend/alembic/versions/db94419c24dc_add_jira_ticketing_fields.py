"""Add Jira ticketing fields (per-cluster credentials, per-incident issue key).

Revision ID: db94419c24dc
Revises: 2253eabf13e3
Create Date: 2026-08-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "db94419c24dc"
down_revision: Union[str, None] = "2253eabf13e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("jira_url", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("jira_email", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("jira_api_token", sa.String(), nullable=True))
    op.add_column("clusters", sa.Column("jira_project_key", sa.String(), nullable=True))
    op.add_column("incidents", sa.Column("jira_issue_key", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "jira_issue_key")
    op.drop_column("clusters", "jira_project_key")
    op.drop_column("clusters", "jira_api_token")
    op.drop_column("clusters", "jira_email")
    op.drop_column("clusters", "jira_url")
