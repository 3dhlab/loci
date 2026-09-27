"""add search tuning reports

Revision ID: 20260328_0015
Revises: 20260328_0014
Create Date: 2026-03-28 23:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260328_0015"
down_revision: Union[str, Sequence[str], None] = "20260328_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


tuning_report_status = postgresql.ENUM(
    "DRAFT",
    "APPROVED_FOR_LIVE_COMPARISON",
    name="tuning_report_status",
)


def upgrade() -> None:
    tuning_report_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "search_tuning_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "DRAFT",
                "APPROVED_FOR_LIVE_COMPARISON",
                name="tuning_report_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("feedback_window_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("new_vote_count", sa.Integer(), nullable=False),
        sa.Column("canonical_query_count", sa.Integer(), nullable=False),
        sa.Column("recurring_canonical_query_count", sa.Integer(), nullable=False),
        sa.Column("positive_vote_count", sa.Integer(), nullable=False),
        sa.Column("negative_vote_count", sa.Integer(), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_search_tuning_reports_created_at"), "search_tuning_reports", ["created_at"], unique=False)
    op.create_index(op.f("ix_search_tuning_reports_status"), "search_tuning_reports", ["status"], unique=False)
    op.create_index(op.f("ix_search_tuning_reports_user_id"), "search_tuning_reports", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_search_tuning_reports_user_id"), table_name="search_tuning_reports")
    op.drop_index(op.f("ix_search_tuning_reports_status"), table_name="search_tuning_reports")
    op.drop_index(op.f("ix_search_tuning_reports_created_at"), table_name="search_tuning_reports")
    op.drop_table("search_tuning_reports")
    tuning_report_status.drop(op.get_bind(), checkfirst=True)
