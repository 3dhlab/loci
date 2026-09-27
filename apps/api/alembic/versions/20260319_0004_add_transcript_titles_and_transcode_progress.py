"""add transcript titles and video transcode progress fields

Revision ID: 20260319_0004
Revises: 20260318_0003
Create Date: 2026-03-19 10:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260319_0004"
down_revision: Union[str, Sequence[str], None] = "20260318_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("transcode_progress_pct", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("transcode_stage", sa.String(length=64), nullable=True))
    op.add_column("videos", sa.Column("transcode_started_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("transcripts", sa.Column("title", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE transcripts
        SET title = COALESCE(NULLIF(videos.title, ''), 'Transcript') || ' Transcript'
        FROM videos
        WHERE transcripts.video_id = videos.id
          AND (transcripts.title IS NULL OR transcripts.title = '');
        """
    )
    op.alter_column("transcripts", "title", existing_type=sa.String(length=255), nullable=False)


def downgrade() -> None:
    op.drop_column("transcripts", "title")
    op.drop_column("videos", "transcode_started_at")
    op.drop_column("videos", "transcode_stage")
    op.drop_column("videos", "transcode_progress_pct")