"""add default camera json to object models

Revision ID: 20260324_0008
Revises: 20260324_0007
Create Date: 2026-03-24 00:08:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260324_0008"
down_revision = "20260324_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("object_models", sa.Column("default_camera_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("object_models", "default_camera_json")