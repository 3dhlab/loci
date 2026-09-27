"""add transcript windows for chunk-first semantic retrieval

Revision ID: 20260328_0012
Revises: 20260328_0011
Create Date: 2026-03-28 18:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "20260328_0012"
down_revision: Union[str, Sequence[str], None] = "20260328_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transcript_windows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("transcript_id", sa.UUID(), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("start_position", sa.Integer(), nullable=False),
        sa.Column("end_position", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding_vector", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transcript_id", "window_index", name="uq_transcript_window_index"),
    )


def downgrade() -> None:
    op.drop_table("transcript_windows")
