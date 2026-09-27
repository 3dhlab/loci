"""Bounded, representation-specific cache identity for public media.

Model URL identity includes the canonical revision, variant publication gates,
and the global kill switch.  Video URL identity comes from the persisted digest
of the normalized playback representation.  This keeps request-time work O(1)
for videos and bounded by the four-key variant allowlist for models; large media
files are never re-hashed while serving a page or body.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _full_digest(parts: list[str]) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_sha256(value: object) -> str | None:
    """Return a canonical persisted SHA-256, or ``None`` for legacy/bad data."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _SHA256_RE.fullmatch(candidate) else None


def canonical_model_revision(model) -> str:
    return f"{model.revision_number}-{model.updated_at.isoformat()}"


def model_variant_state_digest(db: Session, object_model_id: uuid.UUID) -> str:
    """Digest every bounded input that can alter the selected model body."""
    from app.models.entities import ObjectModelVariant

    rows = db.execute(
        select(
            ObjectModelVariant.variant_key,
            ObjectModelVariant.variant_sha256,
            ObjectModelVariant.approval_status,
            ObjectModelVariant.visual_qa_status,
            ObjectModelVariant.device_qa_status,
            ObjectModelVariant.is_public_selectable,
            ObjectModelVariant.source_canonical_sha256,
            ObjectModelVariant.source_revision_number,
            ObjectModelVariant.created_by,
            ObjectModelVariant.approved_by,
            ObjectModelVariant.storage_path,
            ObjectModelVariant.file_size_bytes,
        )
        .where(ObjectModelVariant.object_model_id == object_model_id)
        .order_by(ObjectModelVariant.variant_key)
    ).all()

    parts = [f"switch={'1' if getattr(settings, 'public_model_variants_enabled', False) else '0'}"]
    for row in rows:
        parts.append(
            "|".join(
                (
                    str(row.variant_key),
                    str(row.variant_sha256),
                    str(row.approval_status),
                    str(row.visual_qa_status),
                    str(row.device_qa_status),
                    "1" if row.is_public_selectable else "0",
                    str(row.source_canonical_sha256),
                    str(row.source_revision_number),
                    str(row.created_by),
                    str(row.approved_by),
                    str(row.storage_path),
                    str(row.file_size_bytes),
                )
            )
        )
    return _full_digest(parts)


def model_cache_token(db: Session, model) -> str:
    # Keep timestamps and publication metadata inside an opaque URL-safe
    # identity so query parsers cannot reinterpret timezone characters.
    return _full_digest(
        [
            canonical_model_revision(model),
            str(getattr(model, "sha256_checksum", "")),
            str(getattr(model, "file_size_bytes", "")),
            model_variant_state_digest(db, model.id),
        ]
    )


def video_cache_token(video) -> str:
    """Opaque immutable token for the exact normalized playback rendition.

    A valid persisted digest changes on every byte replacement, including a
    same-size replacement.  ``legacy`` is deliberate for unmigrated rows: the
    delivery endpoint marks those responses revalidation-only until a separately
    gated digest backfill records the artifact identity.
    """
    digest = normalized_sha256(getattr(video, "playback_sha256_checksum", None))
    if digest is None:
        return "legacy"
    return digest
