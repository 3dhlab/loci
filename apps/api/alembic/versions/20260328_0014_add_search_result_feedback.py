"""add search result feedback table

Revision ID: 20260328_0014
Revises: 20260328_0013
Create Date: 2026-03-28 21:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260328_0014"
down_revision: Union[str, Sequence[str], None] = "20260328_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_result_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("search_query_log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_relevant", sa.Boolean(), nullable=False),
        sa.Column("lexical_match", sa.Boolean(), nullable=False),
        sa.Column("semantic_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("rank_score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("rank_position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_query_log_id"], ["search_query_logs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("search_query_log_id", "segment_id", name="uq_search_result_feedback_query_segment"),
    )
    op.create_index(op.f("ix_search_result_feedback_created_at"), "search_result_feedback", ["created_at"], unique=False)
    op.create_index(op.f("ix_search_result_feedback_search_query_log_id"), "search_result_feedback", ["search_query_log_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_search_result_feedback_search_query_log_id"), table_name="search_result_feedback")
    op.drop_index(op.f("ix_search_result_feedback_created_at"), table_name="search_result_feedback")
    op.drop_table("search_result_feedback")
