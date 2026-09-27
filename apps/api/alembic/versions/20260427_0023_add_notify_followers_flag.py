"""add notify_followers_on_publish to object_models

Add an explicit subscriber notification preference.

Adds the per-package "Notify followers on publish" toggle to the
object_models table. When set, publishing the package enqueues a
digest-email job (apps/api/app/services/notify_digest_worker.py) that
fans out to subscribers in the same customer_key partition.

Default is FALSE for explicit
opt-in; the original §5.1 #7 default was opt-out / on-by-default).

Revision ID: 20260427_0023
Revises: 20260427_0022
Create Date: 2026-04-27 13:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260427_0023"
down_revision: Union[str, Sequence[str], None] = "20260427_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "object_models",
        sa.Column(
            "notify_followers_on_publish",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("object_models", "notify_followers_on_publish")
