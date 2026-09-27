"""add persisted object model transform fields

Revision ID: 20260324_0007
Revises: 20260319_0006
Create Date: 2026-03-24 20:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260324_0007"
down_revision: Union[str, Sequence[str], None] = "20260319_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("object_models", sa.Column("position_x", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.add_column("object_models", sa.Column("position_y", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.add_column("object_models", sa.Column("position_z", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.add_column("object_models", sa.Column("rotation_x", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.add_column("object_models", sa.Column("rotation_y", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.add_column("object_models", sa.Column("rotation_z", sa.Numeric(12, 6), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("object_models", "rotation_z")
    op.drop_column("object_models", "rotation_y")
    op.drop_column("object_models", "rotation_x")
    op.drop_column("object_models", "position_z")
    op.drop_column("object_models", "position_y")
    op.drop_column("object_models", "position_x")