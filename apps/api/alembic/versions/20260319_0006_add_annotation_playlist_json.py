"""add playlist json to object model annotations

Revision ID: 20260319_0006
Revises: 20260319_0005
Create Date: 2026-03-19 15:05:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260319_0006"
down_revision: Union[str, Sequence[str], None] = "20260319_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("object_model_annotations", sa.Column("playlist_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.execute(
        """
        UPDATE object_model_annotations
        SET playlist_json = jsonb_build_array(
            jsonb_build_object(
                'video_id', video_id,
                'clip_id', clip_id,
                'transcript_segment_id', transcript_segment_id,
                'label', NULL,
                'start_ms', start_ms,
                'end_ms', end_ms
            )
        )
        WHERE playlist_json IS NULL;
        """
    )


def downgrade() -> None:
    op.drop_column("object_model_annotations", "playlist_json")