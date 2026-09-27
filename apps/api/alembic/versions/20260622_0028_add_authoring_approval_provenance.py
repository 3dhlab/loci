"""add four-eyes provenance columns + readiness_snapshot (authoring approvals)

Revision ID: 20260622_0028
Revises: 20260622_0027
Create Date: 2026-06-22 14:00:00

Additive-only. Adds provenance columns used for four-eyes enforcement
(``proposed_by`` / ``approved_by`` / ``granted_by`` / ``confirmed_by``), an
object-label decision on ``ocr_batch``, an evidence-override ``status``, and a
new ``readiness_snapshot`` table that binds a ``source_version`` to an immutable
content digest. No existing column is altered or dropped; downgrade reverses
exactly these additions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260622_0028"
down_revision: Union[str, Sequence[str], None] = "20260622_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Object-label decision (PM) recorded on the batch (private metadata).
    op.add_column("ocr_batch", sa.Column("approved_object_label", sa.Text(), nullable=True))
    op.add_column("ocr_batch", sa.Column("approved_object_accession", sa.String(length=64), nullable=True))
    op.add_column("ocr_batch", sa.Column("object_label_decided_by", postgresql.UUID(as_uuid=True), nullable=True))

    # Segment proposer (set on create) — enables segment-approve four-eyes.
    op.add_column("title_card_segment", sa.Column("proposed_by", postgresql.UUID(as_uuid=True), nullable=True))

    # Approval actor.
    op.add_column("approval_decision", sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True))

    # Spelling confirmation actor.
    op.add_column("segment_spelling_flag", sa.Column("confirmed_by", postgresql.UUID(as_uuid=True), nullable=True))

    # Evidence-override lifecycle: proposer, grantor, and status.
    op.add_column("evidence_override", sa.Column("proposed_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("evidence_override", sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "evidence_override",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="proposed"),
    )

    op.create_table(
        "readiness_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="speaker_approved"),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("fixture_sha256", sa.String(length=64), nullable=True),
        sa.Column("media_sha256", sa.String(length=64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("checklist_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "source_version", name="uq_readiness_snapshot_batch_source_version"),
    )
    op.create_index("ix_readiness_snapshot_batch", "readiness_snapshot", ["batch_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_readiness_snapshot_batch", table_name="readiness_snapshot")
    op.drop_table("readiness_snapshot")
    op.drop_column("evidence_override", "status")
    op.drop_column("evidence_override", "granted_by")
    op.drop_column("evidence_override", "proposed_by")
    op.drop_column("segment_spelling_flag", "confirmed_by")
    op.drop_column("approval_decision", "approved_by")
    op.drop_column("title_card_segment", "proposed_by")
    op.drop_column("ocr_batch", "object_label_decided_by")
    op.drop_column("ocr_batch", "approved_object_accession")
    op.drop_column("ocr_batch", "approved_object_label")
