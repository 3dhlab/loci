"""drop unique checksum constraint on videos

Revision ID: 20260318_0003
Revises: 20260225_0002
Create Date: 2026-03-18 23:30:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260318_0003"
down_revision: Union[str, Sequence[str], None] = "20260225_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'videos_sha256_checksum_key'
            ) THEN
                ALTER TABLE videos DROP CONSTRAINT videos_sha256_checksum_key;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'videos_sha256_checksum_key'
            ) THEN
                ALTER TABLE videos
                ADD CONSTRAINT videos_sha256_checksum_key UNIQUE (sha256_checksum);
            END IF;
        END
        $$;
        """
    )
