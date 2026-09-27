"""add transcript window vector index and retrieval metrics

Revision ID: 20260328_0013
Revises: 20260328_0012
Create Date: 2026-03-28 20:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260328_0013"
down_revision: Union[str, Sequence[str], None] = "20260328_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("search_query_logs", sa.Column("retrieval_mode", sa.String(length=32), nullable=True))
    op.add_column("search_query_logs", sa.Column("top_window_score", sa.Numeric(precision=12, scale=6), nullable=True))
    op.add_column(
        "search_query_logs",
        sa.Column("top_surfaced_segment_score", sa.Numeric(precision=12, scale=6), nullable=True),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcript_windows_embedding_vector "
        "ON transcript_windows USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transcript_windows_embedding_vector;")
    op.drop_column("search_query_logs", "top_surfaced_segment_score")
    op.drop_column("search_query_logs", "top_window_score")
    op.drop_column("search_query_logs", "retrieval_mode")
