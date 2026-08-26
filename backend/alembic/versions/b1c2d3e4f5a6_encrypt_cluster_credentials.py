"""Encrypt cluster credentials and add safe cluster-token lookup hashes.

Revision ID: b1c2d3e4f5a6
Revises: 9c0d1e2f3a4b
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from backend.crypto import (
    credential_lookup_hash,
    current_key_version,
    decrypt_value,
    encrypt_value,
)


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "9c0d1e2f3a4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CREDENTIAL_COLUMNS = (
    "token",
    "k8s_token",
    "github_token",
    "notion_api_key",
    "llm_api_key",
)


def _clusters_table() -> sa.Table:
    return sa.table(
        "clusters",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("token", sa.Text()),
        sa.column("token_hash", sa.String(length=64)),
        sa.column("key_version", sa.Integer()),
        sa.column("execution_context_version", sa.Integer()),
        sa.column("k8s_token", sa.Text()),
        sa.column("github_token", sa.Text()),
        sa.column("notion_api_key", sa.Text()),
        sa.column("llm_api_key", sa.Text()),
    )


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column("token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "clusters",
        sa.Column("key_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "clusters",
        sa.Column(
            "execution_context_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    op.drop_index("ix_clusters_token", table_name="clusters")
    op.alter_column(
        "clusters", "token", existing_type=sa.String(), type_=sa.Text(), nullable=False
    )
    for column_name in ("github_token", "notion_api_key", "llm_api_key"):
        op.alter_column(
            "clusters",
            column_name,
            existing_type=sa.String(),
            type_=sa.Text(),
            nullable=True,
        )

    bind = op.get_bind()
    clusters = _clusters_table()
    version = current_key_version()
    rows = bind.execute(sa.select(clusters)).mappings().all()
    for row in rows:
        values = {
            "token_hash": credential_lookup_hash(row["token"]),
            "key_version": version,
        }
        for column_name in _CREDENTIAL_COLUMNS:
            if row[column_name] is not None:
                values[column_name] = encrypt_value(row[column_name], version=version)
        bind.execute(
            clusters.update().where(clusters.c.id == row["id"]).values(**values)
        )

    op.alter_column("clusters", "token_hash", nullable=False)
    op.alter_column("clusters", "key_version", nullable=False)
    op.create_index(
        "ix_clusters_token_hash",
        "clusters",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    clusters = _clusters_table()
    rows = bind.execute(sa.select(clusters)).mappings().all()
    for row in rows:
        values = {
            column_name: decrypt_value(row[column_name])
            for column_name in _CREDENTIAL_COLUMNS
            if row[column_name] is not None
        }
        bind.execute(
            clusters.update().where(clusters.c.id == row["id"]).values(**values)
        )

    op.drop_index("ix_clusters_token_hash", table_name="clusters")
    op.drop_column("clusters", "key_version")
    op.drop_column("clusters", "execution_context_version")
    op.drop_column("clusters", "token_hash")

    for column_name in ("github_token", "notion_api_key", "llm_api_key"):
        op.alter_column(
            "clusters",
            column_name,
            existing_type=sa.Text(),
            type_=sa.String(),
            nullable=True,
        )
    op.alter_column(
        "clusters", "token", existing_type=sa.Text(), type_=sa.String(), nullable=False
    )
    op.create_index("ix_clusters_token", "clusters", ["token"], unique=True)
