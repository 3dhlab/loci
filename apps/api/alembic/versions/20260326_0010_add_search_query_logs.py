"""add search query logs

Revision ID: 20260326_0010
Revises: 20260326_0009
Create Date: 2026-03-26 00:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260326_0010"
down_revision: Union[str, Sequence[str], None] = "20260326_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_query_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("query_text", sa.String(length=300), nullable=False),
        sa.Column("query_source", sa.String(length=32), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("lexical_result_count", sa.Integer(), nullable=False),
        sa.Column("semantic_result_count", sa.Integer(), nullable=False),
        sa.Column("semantic_attempted", sa.Boolean(), nullable=False),
        sa.Column("semantic_succeeded", sa.Boolean(), nullable=False),
        sa.Column("semantic_provider", sa.String(length=64), nullable=True),
        sa.Column("semantic_model", sa.String(length=255), nullable=True),
        sa.Column("top_semantic_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_search_query_logs_created_at"), "search_query_logs", ["created_at"], unique=False)
    op.create_index(op.f("ix_search_query_logs_query_text"), "search_query_logs", ["query_text"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_search_query_logs_query_text"), table_name="search_query_logs")
    op.drop_index(op.f("ix_search_query_logs_created_at"), table_name="search_query_logs")
    op.drop_table("search_query_logs")