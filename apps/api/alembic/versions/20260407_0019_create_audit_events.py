"""create audit events

Revision ID: 20260407_0019
Revises: 20260407_0018
Create Date: 2026-04-07 18:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260407_0019"
down_revision: Union[str, Sequence[str], None] = "20260407_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Audit rows are append-only history records, so subject and manifest identifiers remain
    # plain UUID references rather than DB-enforced foreign keys.
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("publication_manifest_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_public_id", sa.String(length=255), nullable=True),
        sa.Column("actor_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)
    op.create_index("ix_audit_events_subject", "audit_events", ["subject_type", "subject_id"], unique=False)
    op.create_index("ix_audit_events_manifest_id", "audit_events", ["publication_manifest_id"], unique=False)
    op.create_index("ix_audit_events_root_public_id", "audit_events", ["root_public_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_events_root_public_id", table_name="audit_events")
    op.drop_index("ix_audit_events_manifest_id", table_name="audit_events")
    op.drop_index("ix_audit_events_subject", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")