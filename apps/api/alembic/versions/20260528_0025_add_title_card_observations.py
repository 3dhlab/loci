"""add title card observations

Revision ID: 20260528_0025
Revises: 20260427_0024
Create Date: 2026-05-28 10:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260528_0025"
down_revision: Union[str, Sequence[str], None] = "20260427_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "title_card_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp_ms", sa.Integer(), nullable=False),
        sa.Column("title_card_visible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("object_name", sa.Text(), nullable=True),
        sa.Column("object_accession_number", sa.Text(), nullable=True),
        sa.Column("object_holding_institution", sa.Text(), nullable=True),
        sa.Column("object_city", sa.Text(), nullable=True),
        sa.Column("object_region", sa.Text(), nullable=True),
        sa.Column("object_country", sa.Text(), nullable=True),
        sa.Column("presenter_name", sa.Text(), nullable=True),
        sa.Column("presenter_age", sa.Integer(), nullable=True),
        sa.Column("presenter_city", sa.Text(), nullable=True),
        sa.Column("presenter_region", sa.Text(), nullable=True),
        sa.Column("presenter_country", sa.Text(), nullable=True),
        sa.Column("public_speaker_label", sa.Text(), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("session_date_text", sa.Text(), nullable=True),
        sa.Column("additional_text", sa.Text(), nullable=True),
        sa.Column("raw_text_observed", sa.Text(), nullable=False),
        sa.Column("raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence_object", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("confidence_presenter", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("confidence_session_date", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.String(length=32), nullable=True),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.String(length=64), nullable=False),
        sa.Column("model_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "video_id",
            "timestamp_ms",
            "source_kind",
            "source_version",
            "prompt_version",
            name="uq_title_card_observations_video_timestamp_version",
        ),
    )
    op.create_index(
        "ix_title_card_observations_video_timestamp",
        "title_card_observations",
        ["video_id", "timestamp_ms"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_title_card_observations_video_timestamp", table_name="title_card_observations")
    op.drop_table("title_card_observations")