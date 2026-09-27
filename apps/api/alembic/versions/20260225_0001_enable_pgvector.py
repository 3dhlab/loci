"""enable pgvector extension

Revision ID: 20260225_0001
Revises:
Create Date: 2026-02-25 00:00:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260225_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
