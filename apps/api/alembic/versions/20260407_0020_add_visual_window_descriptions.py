"""add visual window descriptions

Revision ID: 20260407_0020
Revises: 20260407_0019
Create Date: 2026-04-07 22:15:00
"""

from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260407_0020"
down_revision: Union[str, Sequence[str], None] = "20260407_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "visual_window_descriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transcript_window_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("embedding_vector", Vector(768), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("frame_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("generator_provider", sa.String(length=64), nullable=True),
        sa.Column("generator_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_provider", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["transcript_window_id"], ["transcript_windows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transcript_window_id", name="uq_visual_window_descriptions_transcript_window_id"),
    )
    op.create_index(
        "ix_visual_window_descriptions_transcript_id",
        "visual_window_descriptions",
        ["transcript_id"],
        unique=False,
    )
    op.create_index(
        "ix_visual_window_descriptions_video_id",
        "visual_window_descriptions",
        ["video_id"],
        unique=False,
    )
    op.create_index(
        "ix_visual_window_descriptions_status",
        "visual_window_descriptions",
        ["status"],
        unique=False,
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_visual_window_descriptions_embedding_vector "
        "ON visual_window_descriptions USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_visual_window_descriptions_embedding_vector;")
    op.drop_index("ix_visual_window_descriptions_status", table_name="visual_window_descriptions")
    op.drop_index("ix_visual_window_descriptions_video_id", table_name="visual_window_descriptions")
    op.drop_index("ix_visual_window_descriptions_transcript_id", table_name="visual_window_descriptions")
    op.drop_table("visual_window_descriptions")