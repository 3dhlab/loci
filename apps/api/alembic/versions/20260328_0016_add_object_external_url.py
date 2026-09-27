"""add external url to objects

Revision ID: 20260328_0016
Revises: 20260328_0015
Create Date: 2026-03-28 23:35:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260328_0016"
down_revision: Union[str, Sequence[str], None] = "20260328_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("objects", sa.Column("external_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    op.drop_column("objects", "external_url")
