"""add subscribers table for notify-me capture

Add partitioned subscriber registration.

The subscribers table is partner-scoped (customer_key) so a single Loci
public stack can serve multiple whitelabel partners without cross-list
contamination. Email addresses are stored in plaintext (industry norm
for opt-in subscriber lists; required to support email-link unsubscribe);
no other PII is collected. The unsubscribe_token is a 32-byte URL-safe
random secret per row, unforgeable and not enumerable.

Revision ID: 20260427_0022
Revises: 20260422_0021
Create Date: 2026-04-27 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260427_0022"
down_revision: Union[str, Sequence[str], None] = "20260422_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscribers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Plaintext lowercase-normalized email. Trade-off documented in the
        # SubscribeRequest schema doc: hashing would prevent email-link
        # unsubscribe and migration to other email systems; plaintext is the
        # opt-in subscriber-list industry norm. No other PII is stored
        # alongside, so the data-minimization posture is "email + sparse
        # context + nothing else."
        sa.Column("email", sa.Text(), nullable=False),
        # Whitelabel partition. Always derived server-side from the request
        # Host header via app.core.customer_keys.resolve_customer_key — never
        # from a client-supplied request field. Prevents subscriber-list
        # injection where a client subscribes with another partner's key.
        sa.Column("customer_key", sa.String(length=64), nullable=False),
        # 32-byte URL-safe random per row (~256 bits entropy via
        # secrets.token_urlsafe(32) at row insert time). Used as the only
        # auth credential for unsubscribe + delete endpoints.
        sa.Column("unsubscribe_token", sa.Text(), nullable=False),
        # Optional context fields for high-signal future debugging /
        # optimization. None of these are required; all are nullable.
        sa.Column("source_url", sa.Text(), nullable=True),
        # Coarse user-agent bucket only ("desktop"/"mobile"/"tablet"/"other").
        # Avoids storing the full UA string which is a fingerprinting vector.
        sa.Column("user_agent_class", sa.String(length=16), nullable=True),
        # BCP-47 language tag from the visitor's browser preference.
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Soft-delete via timestamp, NOT a boolean — preserves audit trail
        # of when the subscriber unsubscribed (useful for re-engagement
        # boundaries, GDPR DSAR fulfillment, and digest dedup logic).
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        # Updated by the digest worker after successful per-subscriber
        # send so the 24h-batching gate can read it without joining a sends
        # log. Null when no digest has ever fired for this subscriber.
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "unsubscribe_token", name="uq_subscribers_unsubscribe_token"
        ),
        # email + customer_key uniqueness lets the resubscribe path be
        # idempotent: same email + same partner re-POSTs simply reset
        # unsubscribed_at to NULL rather than creating a duplicate row.
        sa.UniqueConstraint(
            "email", "customer_key", name="uq_subscribers_email_customer"
        ),
    )

    op.create_index(
        "ix_subscribers_customer_key",
        "subscribers",
        ["customer_key"],
        unique=False,
    )
    op.create_index(
        "ix_subscribers_email",
        "subscribers",
        ["email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscribers_email", table_name="subscribers")
    op.drop_index("ix_subscribers_customer_key", table_name="subscribers")
    op.drop_table("subscribers")
