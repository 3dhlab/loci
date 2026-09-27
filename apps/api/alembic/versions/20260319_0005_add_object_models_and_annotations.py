"""add object model and annotation tables

Revision ID: 20260319_0005
Revises: 20260319_0004
Create Date: 2026-03-19 13:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260319_0005"
down_revision: Union[str, Sequence[str], None] = "20260319_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


annotation_review_status = postgresql.ENUM("ACTIVE", "REVIEW_REQUIRED", name="annotation_review_status", create_type=False)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'annotation_review_status') THEN
                CREATE TYPE annotation_review_status AS ENUM ('ACTIVE', 'REVIEW_REQUIRED');
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "object_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("sha256_checksum", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["object_id"], ["objects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_id"),
    )

    op.create_table(
        "object_model_annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transcript_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("point_x", sa.Numeric(12, 6), nullable=False),
        sa.Column("point_y", sa.Numeric(12, 6), nullable=False),
        sa.Column("point_z", sa.Numeric(12, 6), nullable=False),
        sa.Column("normal_x", sa.Numeric(12, 6), nullable=True),
        sa.Column("normal_y", sa.Numeric(12, 6), nullable=True),
        sa.Column("normal_z", sa.Numeric(12, 6), nullable=True),
        sa.Column("camera_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("review_status", annotation_review_status, nullable=False, server_default="ACTIVE"),
        sa.Column("model_revision_created_against", sa.Integer(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["object_model_id"], ["object_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["object_id"], ["objects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transcript_segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("object_model_annotations")
    op.drop_table("object_models")
    annotation_review_status.drop(op.get_bind(), checkfirst=True)