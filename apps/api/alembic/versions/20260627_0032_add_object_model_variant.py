"""add object_model_variant table (dark/default-off optimized model variants)

Additive-only. Creates ``object_model_variant`` for approved, optimized
derivatives of the canonical ``object_models`` asset (e.g. mobile-texture builds).
No rows are created and no public contract changes; serving is gated behind the
``public_model_variants_enabled`` settings kill switch (default off) plus per-row
approval / QA / selectable / canonical-SHA gates enforced in the resolver. The
``variant_key`` CHECK allowlist makes it impossible to store ``ios-low-memory``.
Downgrade drops the table.

Revision ID: 20260627_0032
Revises: 20260625_0031
Create Date: 2026-06-27
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260627_0032"
down_revision: Union[str, Sequence[str], None] = "20260625_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "object_model_variant",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "object_model_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("object_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant_key", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False, server_default=sa.text("'model/gltf-binary'")),
        # Binding to canonical (stale invalidation).
        sa.Column("source_canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_revision_number", sa.Integer(), nullable=False),
        # Asset metrics.
        sa.Column("variant_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_texture_dimension_px", sa.Integer(), nullable=False),
        sa.Column("decoded_texture_ram_bytes", sa.BigInteger(), nullable=False),
        sa.Column("triangle_count", sa.BigInteger(), nullable=False),
        sa.Column("vertex_count", sa.BigInteger(), nullable=False),
        sa.Column("draw_call_count", sa.Integer(), nullable=True),
        sa.Column("geometry_extensions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        # Generator recipe.
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("generator_recipe", sa.JSON(), nullable=False),
        # Visual QA.
        sa.Column("visual_qa_status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("visual_qa_changed_px_ratio", sa.Numeric(precision=6, scale=5), nullable=True),
        sa.Column("visual_qa_mean_delta", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("visual_qa_report_path", sa.String(length=1024), nullable=True),
        sa.Column("visual_qa_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("visual_qa_at", sa.DateTime(timezone=True), nullable=True),
        # Device QA.
        sa.Column("device_qa_status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("device_qa_notes", sa.Text(), nullable=True),
        sa.Column("device_qa_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_qa_at", sa.DateTime(timezone=True), nullable=True),
        # Approval (four-eyes).
        sa.Column("approval_status", sa.String(length=16), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submitted_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        # Public selectability.
        sa.Column("is_public_selectable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("object_model_id", "variant_key", name="uq_object_model_variant_model_key"),
        sa.CheckConstraint(
            "variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096')",
            name="ck_object_model_variant_key_allowlist",
        ),
        sa.CheckConstraint(
            "visual_qa_status IN ('pending', 'passed', 'failed')",
            name="ck_object_model_variant_visual_qa_status",
        ),
        sa.CheckConstraint(
            "device_qa_status IN ('pending', 'passed', 'failed')",
            name="ck_object_model_variant_device_qa_status",
        ),
        sa.CheckConstraint(
            "approval_status IN ('draft', 'submitted', 'approved', 'rejected', 'revoked')",
            name="ck_object_model_variant_approval_status",
        ),
        sa.CheckConstraint(
            "NOT is_public_selectable OR "
            "(approval_status = 'approved' AND visual_qa_status = 'passed' AND device_qa_status = 'passed')",
            name="ck_object_model_variant_selectable_requires_approval",
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by",
            name="ck_object_model_variant_four_eyes_distinct",
        ),
    )
    op.create_index(
        "ix_object_model_variant_selectable",
        "object_model_variant",
        ["object_model_id", "variant_key"],
        unique=False,
        postgresql_where=sa.text("is_public_selectable"),
    )


def downgrade() -> None:
    op.drop_index("ix_object_model_variant_selectable", table_name="object_model_variant")
    op.drop_table("object_model_variant")
