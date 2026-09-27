"""add guarded-import (Stage B4) fields to import_run

Additive-only. Extends ``import_run`` so it can record both PREFLIGHT runs and
guarded IMPORT runs (and their rollback state) for the private authoring lane,
plus the four-eyes / provenance bindings B4 enforces. Every column is nullable
or defaulted, so the upgrade is safe on existing rows and the downgrade simply
drops the added columns. No existing table or public contract is altered.

Revision ID: 20260622_0029
Revises: 20260622_0028
Create Date: 2026-06-22
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260622_0029"
down_revision: Union[str, Sequence[str], None] = "20260622_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (column, type, kwargs) — all additive and nullable/defaulted.
_COLUMNS = [
    ("kind", sa.String(length=16), {"nullable": False, "server_default": "import"}),
    ("mode", sa.String(length=16), {"nullable": False, "server_default": "dry_run"}),
    ("readiness_snapshot_id", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("preflight_run_id", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("content_digest", sa.String(length=64), {"nullable": True}),
    ("fixture_sha256", sa.String(length=64), {"nullable": True}),
    ("media_sha256", sa.String(length=64), {"nullable": True}),
    ("target_video_uuid", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("executed_by", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("approved_by", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("preflight_passed", sa.Boolean(), {"nullable": True}),
    ("rolled_back_at", sa.DateTime(timezone=True), {"nullable": True}),
    ("rolled_back_by", postgresql.UUID(as_uuid=True), {"nullable": True}),
    ("rollback_deleted_count", sa.Integer(), {"nullable": True}),
    ("request_id", sa.String(length=128), {"nullable": True}),
]


def upgrade() -> None:
    for name, type_, kwargs in _COLUMNS:
        op.add_column("import_run", sa.Column(name, type_, **kwargs))
    op.create_index("ix_import_run_kind", "import_run", ["kind"], unique=False)
    op.create_index("ix_import_run_source_version", "import_run", ["source_version"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_import_run_source_version", table_name="import_run")
    op.drop_index("ix_import_run_kind", table_name="import_run")
    for name, _type, _kwargs in reversed(_COLUMNS):
        op.drop_column("import_run", name)
