"""allow fidelity-first web-8192 object model variants

Expands the exact database allowlist with one delivery tier for photogrammetry
assets that require an 8K texture to preserve surface fidelity. All approval,
QA, SHA, confinement, selectability, and four-eyes constraints remain active.

Revision ID: 20260731_0034
Revises: 20260627_0033
Create Date: 2026-07-31
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0034"
down_revision: Union[str, Sequence[str], None] = "20260627_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_object_model_variant_key_allowlist",
        "object_model_variant",
        type_="check",
    )
    op.create_check_constraint(
        "ck_object_model_variant_key_allowlist",
        "object_model_variant",
        "variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192')",
    )


def downgrade() -> None:
    """Narrow the allowlist again.

    This can only succeed when no ``web-8192`` row exists, because PostgreSQL
    validates a new CHECK constraint against the rows already stored. Rehearsed on a
    throwaway database on 2026-07-31: with such a row present the ``ADD CONSTRAINT``
    raises ``CheckViolation`` and alembic's transactional DDL rolls the whole step
    back, leaving the version at ``20260731_0034`` and the wider constraint intact.
    That is safe -- nothing is half-applied and no row is lost -- but the raw error
    surfaces deep inside a SQLAlchemy traceback and reads like a broken migration.

    The precondition below turns that into an instruction. It is deliberately not a
    ``DELETE``: a published variant row carries the author, approver, QA verdicts and
    artifact digest for a public asset, and a schema downgrade is not authority to
    discard that audit trail.

    Rolling back the *delivery behaviour* uses a coordinated client/API change.
    A client that sets ``variant_required=1``; disabling
    ``PUBLIC_MODEL_VARIANTS_ENABLED`` therefore yields a clear recoverable 503
    instead of loading the memory-heavy canonical model. Previously cached media
    bodies can remain fresh until their bounded cache lifetime expires unless an
    approved purge mechanism exists. Restore the prior frontend policy together
    with the prior API when canonical fallback is an explicitly accepted rollback,
    or retain the recoverable viewer-local failure. Use this downgrade only when
    the schema itself must return to ``20260627_0033``.
    """
    connection = op.get_bind()
    blocking_rows = connection.execute(
        sa.text("SELECT count(*) FROM object_model_variant WHERE variant_key = 'web-8192'")
    ).scalar_one()
    if blocking_rows:
        raise RuntimeError(
            f"Cannot downgrade 20260731_0034: {blocking_rows} object_model_variant "
            "row(s) still use variant_key='web-8192', which the narrower CHECK "
            "constraint forbids.\n"
            "To stop new variant delivery without a schema downgrade, disable "
            "PUBLIC_MODEL_VARIANTS_ENABLED or revoke both required tiers together; "
            "clients requesting an exact variant then presents a recoverable model error.\n"
            "To downgrade the schema, first record the row's approver, QA verdicts "
            "and variant_sha256 in the release artifact, then delete it explicitly "
            "and re-run this downgrade."
        )

    op.drop_constraint(
        "ck_object_model_variant_key_allowlist",
        "object_model_variant",
        type_="check",
    )
    op.create_check_constraint(
        "ck_object_model_variant_key_allowlist",
        "object_model_variant",
        "variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096')",
    )
