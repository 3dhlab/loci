"""add persisted normalized-video playback identity

The existing ``videos.sha256_checksum`` identifies the preservation/source
upload.  A normalized H.264 playback rendition has different bytes and needs an
independent persisted digest for strong HTTP validators and immutable URL
identity.  All columns are nullable so the migration is additive; legacy rows
use the delivery layer's explicitly weak, revalidation-only fallback until a
separately approved backfill records their playback identity.

Revision ID: 20260811_0035
Revises: 20260731_0034
Create Date: 2026-08-11
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0035"
down_revision: Union[str, Sequence[str], None] = "20260731_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("playback_sha256_checksum", sa.String(length=64), nullable=True))
    op.add_column("videos", sa.Column("playback_file_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("videos", sa.Column("playback_recipe_version", sa.String(length=64), nullable=True))
    op.add_column("videos", sa.Column("playback_color_policy", sa.String(length=64), nullable=True))
    op.add_column("videos", sa.Column("playback_storage_key", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "playback_storage_key")
    op.drop_column("videos", "playback_color_policy")
    op.drop_column("videos", "playback_recipe_version")
    op.drop_column("videos", "playback_file_size_bytes")
    op.drop_column("videos", "playback_sha256_checksum")
