"""add real-import (Stage B5) provenance fields to import_run

Additive-only. Records the confined fixture path the preflight bound, the
pre-import backup artifact (path/sha/row count), and the observed
``target_sv_rows`` so a guarded import and its rollback are fully reconstructable
from the row. Every column is nullable; downgrade drops them. No existing table
or public contract is altered.

Revision ID: 20260622_0030
Revises: 20260622_0029
Create Date: 2026-06-22
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260622_0030"
down_revision: Union[str, Sequence[str], None] = "20260622_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("fixture_path", sa.Text(), {"nullable": True}),
    ("backup_path", sa.Text(), {"nullable": True}),
    ("backup_sha256", sa.String(length=64), {"nullable": True}),
    ("backup_row_count", sa.Integer(), {"nullable": True}),
    ("target_sv_rows", sa.Integer(), {"nullable": True}),
]


def upgrade() -> None:
    for name, type_, kwargs in _COLUMNS:
        op.add_column("import_run", sa.Column(name, type_, **kwargs))


def downgrade() -> None:
    for name, _type, _kwargs in reversed(_COLUMNS):
        op.drop_column("import_run", name)
