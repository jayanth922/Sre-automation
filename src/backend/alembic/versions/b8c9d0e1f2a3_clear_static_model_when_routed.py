"""Clear the static model on clusters that use dynamic model routing.

With the router on, the router picks a model per task and the stored model is
not a fallback: the router took it as the anchor for every tier, so a model
saved before routing was switched on quietly steered which models ran. The
update path now drops it on save; this clears rows saved before that.

Irreversible on purpose: the cleared values are not kept anywhere, and
restoring them would bring back the anchoring the change removes.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE clusters "
        "SET llm_model = NULL, "
        "execution_context_version = COALESCE(execution_context_version, 0) + 1 "
        "WHERE llm_router_enabled AND llm_model IS NOT NULL"
    )


def downgrade() -> None:
    pass
