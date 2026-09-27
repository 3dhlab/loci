"""add private authoring/review lane tables

Revision ID: 20260619_0026
Revises: 20260528_0025
Create Date: 2026-06-19 09:00:00

Additive-only. Creates the nine private authoring/review-lane tables that back
the title-card OCR -> review -> approval -> import workflow. No existing table
is modified and no data is migrated; downgrade drops only these tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260619_0026"
down_revision: Union[str, Sequence[str], None] = "20260528_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ocr_batch",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("website_object_id", sa.String(length=128), nullable=False),
        sa.Column("object_title", sa.Text(), nullable=True),
        sa.Column("stable_video_id", sa.String(length=128), nullable=False),
        sa.Column("production_video_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_slug", sa.String(length=255), nullable=True),
        sa.Column("video_title", sa.Text(), nullable=True),
        sa.Column("source_media_host_path", sa.Text(), nullable=True),
        sa.Column("source_media_sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("expected_frame_count", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.String(length=32), nullable=True),
        sa.Column("source_version_raw", sa.String(length=128), nullable=True),
        sa.Column("source_version_date_only", sa.String(length=128), nullable=True),
        sa.Column("source_version_speaker_approved", sa.String(length=128), nullable=True),
        sa.Column("artifact_dir", sa.Text(), nullable=False),
        sa.Column("created_on", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_object_id", "stable_video_id", name="uq_ocr_batch_object_video"),
    )
    op.create_index("ix_ocr_batch_stable_video_id", "ocr_batch", ["stable_video_id"], unique=False)

    op.create_table(
        "ocr_batch_frame",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("frame_filename", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "timestamp_ms", name="uq_ocr_batch_frame_batch_timestamp"),
    )
    op.create_index("ix_ocr_batch_frame_batch", "ocr_batch_frame", ["batch_id"], unique=False)

    op.create_table(
        "observation_review",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("title_card_visible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("object_name_raw", sa.Text(), nullable=True),
        sa.Column("presenter_name_raw", sa.Text(), nullable=True),
        sa.Column("session_date_text", sa.Text(), nullable=True),
        sa.Column("confidence_object", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("confidence_presenter", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("confidence_session_date", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("additional_text", sa.Text(), nullable=True),
        sa.Column("raw_text_observed", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("public_speaker_label", sa.Text(), nullable=True),
        sa.Column("included_in_date_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("included_in_speaker_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_suppressed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suppression_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "timestamp_ms", name="uq_observation_review_batch_timestamp"),
    )
    op.create_index(
        "ix_observation_review_batch_timestamp", "observation_review", ["batch_id", "timestamp_ms"], unique=False
    )

    op.create_table(
        "title_card_segment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segment_label", sa.String(length=32), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms_exclusive", sa.Integer(), nullable=True),
        sa.Column("start_clock", sa.String(length=16), nullable=True),
        sa.Column("end_clock", sa.String(length=16), nullable=True),
        sa.Column("raw_ocr_name", sa.Text(), nullable=True),
        sa.Column("session_date_text", sa.Text(), nullable=True),
        sa.Column("ready_row_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "segment_label", name="uq_title_card_segment_batch_label"),
    )
    op.create_index("ix_title_card_segment_batch", "title_card_segment", ["batch_id"], unique=False)

    op.create_table(
        "approval_decision",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_public_speaker_label", sa.Text(), nullable=True),
        sa.Column("approval_status", sa.String(length=32), nullable=False, server_default="pending_review"),
        sa.Column("approved_canonical_object_label", sa.Text(), nullable=True),
        sa.Column("approved_canonical_accession", sa.String(length=64), nullable=True),
        sa.Column("session_date_text", sa.Text(), nullable=True),
        sa.Column("excluded_internal_timestamps_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("restored_evidence_override_timestamps_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reviewer_source", sa.String(length=128), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("date_approved", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["title_card_segment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("segment_id", name="uq_approval_decision_segment"),
    )
    op.create_index("ix_approval_decision_batch", "approval_decision", ["batch_id"], unique=False)

    op.create_table(
        "segment_spelling_flag",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("field", sa.String(length=128), nullable=True),
        sa.Column("issue", sa.Text(), nullable=True),
        sa.Column("variants_observed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("variant_timestamps_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proposed_canonical", sa.Text(), nullable=True),
        sa.Column("confirmed_canonical", sa.Text(), nullable=True),
        sa.Column("reviewer_confirmation_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["title_card_segment.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_segment_spelling_flag_batch", "segment_spelling_flag", ["batch_id"], unique=False)

    op.create_table(
        "evidence_override",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=True),
        sa.Column("approved_on", sa.String(length=32), nullable=True),
        sa.Column("reviewer_source", sa.String(length=128), nullable=True),
        sa.Column("session_date_text", sa.Text(), nullable=True),
        sa.Column("speaker_band", sa.Text(), nullable=True),
        sa.Column("raw_ocr_caveat", sa.Text(), nullable=True),
        sa.Column("handling", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["title_card_segment.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "timestamp_ms", name="uq_evidence_override_batch_timestamp"),
    )
    op.create_index("ix_evidence_override_batch", "evidence_override", ["batch_id"], unique=False)

    op.create_table(
        "import_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="speaker_approved"),
        sa.Column("source_version", sa.String(length=128), nullable=False),
        sa.Column("model_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("inserted_count", sa.Integer(), nullable=True),
        sa.Column("updated_count", sa.Integer(), nullable=True),
        sa.Column("removed_count", sa.Integer(), nullable=True),
        sa.Column("observations_written", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="recorded"),
        sa.Column("closeout_path", sa.Text(), nullable=True),
        sa.Column("imported_on", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["ocr_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_run_batch", "import_run", ["batch_id"], unique=False)

    op.create_table(
        "import_run_check",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("check_name", sa.String(length=128), nullable=False),
        sa.Column("check_status", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["import_run_id"], ["import_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_run_check_run", "import_run_check", ["import_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_import_run_check_run", table_name="import_run_check")
    op.drop_table("import_run_check")
    op.drop_index("ix_import_run_batch", table_name="import_run")
    op.drop_table("import_run")
    op.drop_index("ix_evidence_override_batch", table_name="evidence_override")
    op.drop_table("evidence_override")
    op.drop_index("ix_segment_spelling_flag_batch", table_name="segment_spelling_flag")
    op.drop_table("segment_spelling_flag")
    op.drop_index("ix_approval_decision_batch", table_name="approval_decision")
    op.drop_table("approval_decision")
    op.drop_index("ix_title_card_segment_batch", table_name="title_card_segment")
    op.drop_table("title_card_segment")
    op.drop_index("ix_observation_review_batch_timestamp", table_name="observation_review")
    op.drop_table("observation_review")
    op.drop_index("ix_ocr_batch_frame_batch", table_name="ocr_batch_frame")
    op.drop_table("ocr_batch_frame")
    op.drop_index("ix_ocr_batch_stable_video_id", table_name="ocr_batch")
    op.drop_table("ocr_batch")
