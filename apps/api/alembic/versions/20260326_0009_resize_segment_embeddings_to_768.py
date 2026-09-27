"""resize segment embeddings to native 768 dimensions

Revision ID: 20260326_0009
Revises: 20260324_0008
Create Date: 2026-03-26 00:00:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260326_0009"
down_revision: Union[str, Sequence[str], None] = "20260324_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_segments_embedding_vector;")
    op.execute("UPDATE segments SET embedding_vector = NULL WHERE embedding_vector IS NOT NULL;")
    op.execute(
        "ALTER TABLE segments "
        "ALTER COLUMN embedding_vector TYPE vector(768) USING NULL::vector(768);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_segments_embedding_vector "
        "ON segments USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_segments_embedding_vector;")
    op.execute("UPDATE segments SET embedding_vector = NULL WHERE embedding_vector IS NOT NULL;")
    op.execute(
        "ALTER TABLE segments "
        "ALTER COLUMN embedding_vector TYPE vector(1536) USING NULL::vector(1536);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_segments_embedding_vector "
        "ON segments USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100);"
    )