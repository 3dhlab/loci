"""add generic per-object optimized-model delivery profiles

Profiles are additive and empty by default. They map the fixed public client
categories (standard and constrained) to globally allowlisted variant tokens.
Artifact identity and approval remain in object_model_variant. Downgrade refuses
to remove an active public contract; operators must deactivate profiles first.

Revision ID: 20260813_0036
Revises: 20260811_0035
Create Date: 2026-08-13
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0036"
down_revision: Union[str, Sequence[str], None] = "20260811_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOKENS = "'mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192'"


def upgrade() -> None:
    op.create_table(
        "object_model_delivery_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "object_model_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("object_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("standard_variant_key", sa.String(length=64), nullable=False),
        sa.Column("constrained_variant_key", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("activated_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("object_model_id", name="uq_object_model_delivery_profile_model"),
        sa.CheckConstraint(
            f"standard_variant_key IN ({_TOKENS})",
            name="ck_object_model_delivery_profile_standard_key",
        ),
        sa.CheckConstraint(
            f"constrained_variant_key IN ({_TOKENS})",
            name="ck_object_model_delivery_profile_constrained_key",
        ),
        sa.CheckConstraint(
            "standard_variant_key <> constrained_variant_key",
            name="ck_object_model_delivery_profile_distinct_keys",
        ),
        sa.CheckConstraint(
            "NOT is_active OR (activated_by IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_object_model_delivery_profile_active_provenance",
        ),
    )
    op.create_index(
        "ix_object_model_delivery_profile_active",
        "object_model_delivery_profiles",
        ["object_model_id"],
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    active_count = bind.execute(
        sa.text("SELECT count(*) FROM object_model_delivery_profiles WHERE is_active")
    ).scalar_one()
    if active_count:
        raise RuntimeError("deactivate all object model delivery profiles before downgrading 0036")
    op.drop_index("ix_object_model_delivery_profile_active", table_name="object_model_delivery_profiles")
    op.drop_table("object_model_delivery_profiles")
