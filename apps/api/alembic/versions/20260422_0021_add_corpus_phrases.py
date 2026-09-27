"""add corpus phrases table and pg_trgm extension

Revision ID: 20260422_0021
Revises: 20260407_0020
Create Date: 2026-04-22 10:00:00
"""

from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260422_0021"
down_revision: Union[str, Sequence[str], None] = "20260407_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    op.create_table(
        "corpus_phrases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("phrase_text", sa.Text(), nullable=False),
        sa.Column("normalized_phrase", sa.Text(), nullable=False),
        sa.Column("embedding_vector", Vector(768), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("corpus_density", sa.Numeric(6, 4), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "normalized_phrase", "source", name="uq_corpus_phrases_project_phrase_source"
        ),
    )
    op.create_index(
        "ix_corpus_phrases_project_id",
        "corpus_phrases",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_corpus_phrases_source",
        "corpus_phrases",
        ["source"],
        unique=False,
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_corpus_phrases_phrase_trgm "
        "ON corpus_phrases USING gin (normalized_phrase gin_trgm_ops);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_corpus_phrases_embedding_vector "
        "ON corpus_phrases USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_corpus_phrases_embedding_vector;")
    op.execute("DROP INDEX IF EXISTS ix_corpus_phrases_phrase_trgm;")
    op.drop_index("ix_corpus_phrases_source", table_name="corpus_phrases")
    op.drop_index("ix_corpus_phrases_project_id", table_name="corpus_phrases")
    op.drop_table("corpus_phrases")
