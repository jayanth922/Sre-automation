"""Add incident timeline events

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-09 00:00:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "incident_timeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("speaker_role", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("incident_id", "sequence", name="uq_incident_timeline_events_incident_sequence"),
    )
    op.create_index(
        "ix_incident_timeline_events_incident_id",
        "incident_timeline_events",
        ["incident_id"],
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, summary, created_at, resolved_at
            FROM incidents
            WHERE summary IS NOT NULL AND summary <> ''
            ORDER BY created_at ASC
            """
        )
    ).mappings().all()

    for row in rows:
        event_timestamp = row["resolved_at"] or row["created_at"]
        bind.execute(
            sa.text(
                """
                INSERT INTO incident_timeline_events (
                    id,
                    incident_id,
                    sequence,
                    event_type,
                    speaker_role,
                    title,
                    content,
                    payload_json,
                    created_at
                ) VALUES (
                    :id,
                    :incident_id,
                    :sequence,
                    :event_type,
                    :speaker_role,
                    :title,
                    :content,
                    :payload_json,
                    :created_at
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "incident_id": row["id"],
                "sequence": 1,
                "event_type": "summary",
                "speaker_role": "supervisor",
                "title": "Supervisor",
                "content": row["summary"],
                "payload_json": "{\"source\": \"migration_backfill\"}",
                "created_at": event_timestamp,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_incident_timeline_events_incident_id", table_name="incident_timeline_events")
    op.drop_table("incident_timeline_events")
