"""add users.is_platform_admin + bootstrap the single-user account

Additive-only. Adds a boolean platform-admin flag to ``users`` (default false)
so an admin-only authoring role-grant surface can replace manual ``project_role``
seeding. The data step bootstraps the EARLIEST-created account as platform admin
(the single-user MVP bootstrap) so existing local/staged DBs get one working
admin without manual SQL. Downgrade drops the column.

Revision ID: 20260625_0031
Revises: 20260622_0030
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260625_0031"
down_revision: Union[str, Sequence[str], None] = "20260622_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Bootstrap: promote the earliest-created account (the single-user MVP
    # bootstrap user) to platform admin so the admin surface is usable without
    # manual seeding. Only promotes when no admin exists yet.
    op.execute(
        """
        UPDATE users
        SET is_platform_admin = true
        WHERE id = (SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 1)
          AND NOT EXISTS (SELECT 1 FROM users WHERE is_platform_admin = true)
        """
    )


def downgrade() -> None:
    op.drop_column("users", "is_platform_admin")
