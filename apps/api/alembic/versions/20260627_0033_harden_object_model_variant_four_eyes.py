"""harden object_model_variant four-eyes: approved variants require an approver

Additive-only security hardening. Adds a CHECK so any ``approved`` variant must
carry a non-null ``approved_by``. Combined with the existing
``ck_object_model_variant_selectable_requires_approval`` (selectable => approved)
and ``ck_object_model_variant_four_eyes_distinct`` (approver <> author), a
publicly selectable variant always has a present approver distinct from the
author — closing the unattributable-approval gap. No data change (table is dark
and empty); downgrade drops the constraint.

Revision ID: 20260627_0033
Revises: 20260627_0032
Create Date: 2026-06-27
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "20260627_0033"
down_revision: Union[str, Sequence[str], None] = "20260627_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_object_model_variant_approved_requires_approver",
        "object_model_variant",
        "approval_status <> 'approved' OR approved_by IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_object_model_variant_approved_requires_approver",
        "object_model_variant",
        type_="check",
    )
