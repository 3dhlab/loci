"""add project-scoped role grants (authoring RBAC)

Revision ID: 20260622_0027
Revises: 20260619_0026
Create Date: 2026-06-22 10:00:00

Additive-only. Creates the ``project_role`` grant table that backs the private
authoring API RBAC layer. No existing table is modified; downgrade drops only
this table. Roles are scoped by ``project_key`` (the batch's project_slug when
present, else its stable_video_id, so objects with a null project_slug
remain grantable).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260622_0027"
down_revision: Union[str, Sequence[str], None] = "20260619_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLES = ("uploader", "reviewer", "approver", "pm", "operator", "auditor")


def upgrade() -> None:
    op.create_table(
        "project_role",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_key", sa.String(length=255), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_key", "role", name="uq_project_role_user_project_role"),
        sa.CheckConstraint(
            "role IN ('" + "', '".join(_ROLES) + "')",
            name="ck_project_role_role_valid",
        ),
    )
    op.create_index("ix_project_role_user", "project_role", ["user_id"], unique=False)
    op.create_index("ix_project_role_project_key", "project_role", ["project_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_project_role_project_key", table_name="project_role")
    op.drop_index("ix_project_role_user", table_name="project_role")
    op.drop_table("project_role")
