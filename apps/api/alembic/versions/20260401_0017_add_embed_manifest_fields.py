"""add embed manifest fields to core public records

Revision ID: 20260401_0017
Revises: 20260328_0016
Create Date: 2026-04-01 13:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260401_0017"
down_revision: Union[str, Sequence[str], None] = "20260328_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("website_slug", sa.String(length=255), nullable=True))
    op.create_index("ix_projects_website_slug", "projects", ["website_slug"], unique=True)

    op.add_column("objects", sa.Column("website_object_id", sa.String(length=255), nullable=True))
    op.add_column("objects", sa.Column("is_embed_ready", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_objects_website_object_id", "objects", ["website_object_id"], unique=True)

    op.add_column("object_models", sa.Column("public_poster_path", sa.String(length=1024), nullable=True))

    op.add_column("object_model_annotations", sa.Column("website_annotation_id", sa.String(length=255), nullable=True))
    op.add_column(
        "object_model_annotations",
        sa.Column(
            "website_relations_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{\"publication_ids\": [], \"project_ids\": [], \"location_ids\": []}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_object_model_annotations_website_annotation_id",
        "object_model_annotations",
        ["website_annotation_id"],
        unique=True,
    )

    op.add_column("clips", sa.Column("website_clip_id", sa.String(length=255), nullable=True))
    op.create_index("ix_clips_website_clip_id", "clips", ["website_clip_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_clips_website_clip_id", table_name="clips")
    op.drop_column("clips", "website_clip_id")

    op.drop_index("ix_object_model_annotations_website_annotation_id", table_name="object_model_annotations")
    op.drop_column("object_model_annotations", "website_relations_json")
    op.drop_column("object_model_annotations", "website_annotation_id")

    op.drop_column("object_models", "public_poster_path")

    op.drop_index("ix_objects_website_object_id", table_name="objects")
    op.drop_column("objects", "is_embed_ready")
    op.drop_column("objects", "website_object_id")

    op.drop_index("ix_projects_website_slug", table_name="projects")
    op.drop_column("projects", "website_slug")