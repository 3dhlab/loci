"""add publication state to core public-facing records

Revision ID: 20260328_0011
Revises: 20260326_0010
Create Date: 2026-03-28 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260328_0011"
down_revision: Union[str, Sequence[str], None] = "20260326_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("objects", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("videos", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("transcripts", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("object_models", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("object_model_annotations", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.alter_column("objects", "is_published", server_default=None)
    op.alter_column("videos", "is_published", server_default=None)
    op.alter_column("transcripts", "is_published", server_default=None)
    op.alter_column("object_models", "is_published", server_default=None)
    op.alter_column("object_model_annotations", "is_published", server_default=None)


def downgrade() -> None:
    op.drop_column("object_model_annotations", "is_published")
    op.drop_column("object_models", "is_published")
    op.drop_column("transcripts", "is_published")
    op.drop_column("videos", "is_published")
    op.drop_column("objects", "is_published")
