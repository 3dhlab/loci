"""create publication manifests

Revision ID: 20260407_0018
Revises: 20260401_0017
Create Date: 2026-04-07 16:15:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260407_0018"
down_revision: Union[str, Sequence[str], None] = "20260401_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep source UUID references as plain columns rather than FKs so package history can
    # survive later source-row deletion or replacement in the authoring database.
    op.create_table(
        "publication_manifests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("package_type", sa.String(length=64), nullable=False),
        sa.Column("root_entity_type", sa.String(length=32), nullable=False),
        sa.Column("root_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_public_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_public_slug", sa.String(length=255), nullable=True),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("public_title", sa.String(length=255), nullable=True),
        sa.Column("public_summary", sa.Text(), nullable=True),
        sa.Column("public_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("internal_linkage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("media_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("projection_digest", sa.String(length=64), nullable=False),
        sa.Column("source_environment", sa.String(length=64), nullable=False),
        sa.Column("destination_environment", sa.String(length=64), nullable=False),
        sa.Column("source_release_tag", sa.String(length=255), nullable=True),
        sa.Column("actor_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("schema_version >= 1", name="ck_publication_manifests_schema_version"),
        sa.CheckConstraint("package_type = 'object_package'", name="ck_publication_manifests_package_type"),
        sa.CheckConstraint("root_entity_type = 'object'", name="ck_publication_manifests_root_entity_type"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_publication_manifests_current_package",
        "publication_manifests",
        ["package_type", "root_public_id"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )
    op.create_index(
        "ix_publication_manifests_root_entity_id",
        "publication_manifests",
        ["root_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_publication_manifests_projection_digest",
        "publication_manifests",
        ["projection_digest"],
        unique=False,
    )
    op.create_index(
        "ix_publication_manifests_generated_at",
        "publication_manifests",
        ["generated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_publication_manifests_generated_at", table_name="publication_manifests")
    op.drop_index("ix_publication_manifests_projection_digest", table_name="publication_manifests")
    op.drop_index("ix_publication_manifests_root_entity_id", table_name="publication_manifests")
    op.drop_index("uq_publication_manifests_current_package", table_name="publication_manifests")
    op.drop_table("publication_manifests")