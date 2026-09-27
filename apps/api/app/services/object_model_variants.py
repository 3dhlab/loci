from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

# The poisoned token from the iOS low-memory regression. It must NEVER resolve to
# a non-canonical asset again. It is blocked at the resolver (below) and cannot be
# stored in object_model_variant (DB CHECK allowlist).
IOS_LOW_MEMORY_MODEL_VARIANT = "ios-low-memory"
PERMANENTLY_BLOCKED_VARIANTS: frozenset[str] = frozenset({IOS_LOW_MEMORY_MODEL_VARIANT})

# Allowlisted optimized variant keys. Kept in sync with the DB CHECK constraint on
# object_model_variant.variant_key.
ALLOWED_VARIANT_KEYS: frozenset[str] = frozenset({"mobile-1024", "mobile-2048", "mobile-4096", "web-8192"})

APPROVED_STATUS = "approved"
QA_PASSED_STATUS = "passed"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def normalize_variant_key(variant: str | None) -> str:
    """Lowercase + trim a requested variant token. Returns '' for None/blank.

    Normalization makes the blocklist/allowlist case- and whitespace-insensitive
    so ``"  IOS-Low-Memory "`` is treated identically to ``"ios-low-memory"``.
    """
    if not isinstance(variant, str):
        return ""
    return variant.strip().lower()


def is_blocked_variant(variant: str | None) -> bool:
    return normalize_variant_key(variant) in PERMANENTLY_BLOCKED_VARIANTS


def is_allowed_variant_key(variant: str | None) -> bool:
    return normalize_variant_key(variant) in ALLOWED_VARIANT_KEYS


def resolve_object_model_variant_path(storage_path: str, variant: str | None) -> Path:
    """Legacy pure resolver — ALWAYS returns the canonical path.

    Retained as the permanent ios-low-memory guard: any stale request (including
    ``variant=ios-low-memory``) resolves to the canonical model. The DB-aware
    public selection lives in :func:`resolve_public_model_path`.
    """
    return Path(storage_path)


@dataclass(frozen=True)
class PublicRequiredVariantArtifact:
    """Persisted identity needed to validate one member of a required tier set.

    The tuple attached to the requested candidate stays bounded by the configured
    object contract (two fixed client categories). Runtime validation uses
    metadata and a filesystem stat for each member; content hashing remains an
    activation-time operation.
    """

    variant_key: str
    storage_path: str
    source_canonical_sha256: str
    variant_sha256: str
    file_size_bytes: int


@dataclass(frozen=True)
class PublicVariantCandidate:
    """The minimal, server-controlled fields the public resolver inspects. Built
    from a persisted ``ObjectModelVariant`` row; ``storage_path`` is never derived
    from client input, so a requested variant token cannot drive path traversal."""

    variant_key: str
    storage_path: str
    approval_status: str
    visual_qa_status: str
    device_qa_status: str
    is_public_selectable: bool
    source_canonical_sha256: str
    variant_sha256: str
    file_size_bytes: int
    created_by: str | None = None
    approved_by: str | None = None
    required_variant_keys: frozenset[str] = frozenset()
    required_variant_artifacts: tuple[PublicRequiredVariantArtifact, ...] = ()


@dataclass(frozen=True)
class PublicModelRepresentation:
    """Resolved public representation with cache identity and failure state.

    ``required_variant_unavailable`` deliberately carries no reason, inventory,
    or storage detail. The public endpoint can map it to one generic recoverable
    response while ordinary legacy requests retain canonical fallback behavior.
    """

    path: Path
    content_sha256: str | None
    expected_size_bytes: int | None
    is_variant: bool
    required_variant_unavailable: bool = False


@dataclass(frozen=True)
class PublicModelDeliveryCapability:
    """Public-safe active tier mapping for the two fixed client categories."""

    standard_variant: str
    constrained_variant: str

    @property
    def required_variant_keys(self) -> frozenset[str]:
        return frozenset({self.standard_variant, self.constrained_variant})

    def public_payload(self, *, camel_case: bool = False) -> dict:
        category_key = "clientCategory" if camel_case else "client_category"
        exact_key = "exactRequired" if camel_case else "exact_required"
        return {
            exact_key: True,
            "tiers": [
                {category_key: "standard", "variant": self.standard_variant},
                {category_key: "constrained", "variant": self.constrained_variant},
            ],
        }


def _profile_variant_keys(profile) -> frozenset[str]:
    if profile is None:
        return frozenset()
    keys = frozenset(
        {
            normalize_variant_key(profile.standard_variant_key),
            normalize_variant_key(profile.constrained_variant_key),
        }
    )
    if len(keys) != 2 or not keys.issubset(ALLOWED_VARIANT_KEYS):
        return frozenset()
    return keys


def active_delivery_profile_tokens(db: Session, object_model_id: uuid.UUID) -> frozenset[str]:
    """Return the configured active tier set without exposing artifact state."""

    from app.models.entities import ObjectModelDeliveryProfile

    profile = db.execute(
        select(ObjectModelDeliveryProfile).where(
            ObjectModelDeliveryProfile.object_model_id == object_model_id,
            ObjectModelDeliveryProfile.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return _profile_variant_keys(profile) if profile is not None else frozenset()


def lookup_public_delivery_capability(
    db: Session,
    object_model_id: uuid.UUID,
    *,
    variants_enabled: bool,
) -> PublicModelDeliveryCapability | None:
    """Return only a complete active, approved, selectable public tier mapping.

    The payload contains fixed tokens and client categories only. Filesystem and
    approval provenance remain private and are revalidated by the byte endpoint.
    """

    if not variants_enabled:
        return None

    from app.models.entities import ObjectModelDeliveryProfile, ObjectModelVariant

    profile = db.execute(
        select(ObjectModelDeliveryProfile).where(
            ObjectModelDeliveryProfile.object_model_id == object_model_id,
            ObjectModelDeliveryProfile.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if profile is None:
        return None
    required_keys = _profile_variant_keys(profile)
    if not required_keys:
        return None
    rows = list(
        db.execute(
            select(ObjectModelVariant.variant_key).where(
                ObjectModelVariant.object_model_id == object_model_id,
                ObjectModelVariant.variant_key.in_(required_keys),
                ObjectModelVariant.approval_status == APPROVED_STATUS,
                ObjectModelVariant.visual_qa_status == QA_PASSED_STATUS,
                ObjectModelVariant.device_qa_status == QA_PASSED_STATUS,
                ObjectModelVariant.is_public_selectable.is_(True),
            )
        ).scalars()
    )
    if frozenset(normalize_variant_key(key) for key in rows) != required_keys:
        return None
    return PublicModelDeliveryCapability(
        standard_variant=normalize_variant_key(profile.standard_variant_key),
        constrained_variant=normalize_variant_key(profile.constrained_variant_key),
    )


def _default_file_exists(path: Path) -> bool:
    return Path(path).is_file()


def _default_file_size(path: Path) -> int:
    return Path(path).stat().st_size


def _is_within_media_root(storage_path: str, media_root: str | None) -> bool:
    """True only if ``storage_path`` resolves strictly under ``media_root``.

    Resolves both sides (``strict=False`` so non-existent files are handled) so
    absolute escapes (``/etc/passwd``) and ``..`` traversal both fail. ``media_root``
    of None/blank means "no confinement configured" -> treated as NOT confined, so
    the caller fails closed to canonical rather than serve an unconfined path.
    """
    if not media_root:
        return False
    try:
        root = Path(media_root).resolve(strict=False)
        resolved = Path(storage_path).resolve(strict=False)
        resolved.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _required_variant_files_are_ready(
    *,
    candidate: PublicVariantCandidate,
    requested_key: str,
    canonical_sha256: str | None,
    media_root: str | None,
    file_exists: Callable[[Path], bool],
    file_size: Callable[[Path], int],
) -> bool:
    """Validate every filesystem member of an exact required-tier contract.

    This remains constant work per request because the configured set is fixed
    and small. It intentionally checks persisted identity plus regular-file
    presence and exact size, and performs no request-time content hashing.
    """

    required_keys = frozenset(normalize_variant_key(key) for key in candidate.required_variant_keys)
    artifacts = candidate.required_variant_artifacts
    if not required_keys and not artifacts:
        return True
    if not required_keys or not artifacts or requested_key not in required_keys:
        return False

    artifacts_by_key: dict[str, PublicRequiredVariantArtifact] = {}
    for artifact in artifacts:
        artifact_key = normalize_variant_key(artifact.variant_key)
        if artifact_key in artifacts_by_key:
            return False
        artifacts_by_key[artifact_key] = artifact
    if artifacts_by_key.keys() != required_keys:
        return False

    requested_artifact = artifacts_by_key[requested_key]
    if (
        requested_artifact.storage_path != candidate.storage_path
        or requested_artifact.variant_sha256 != candidate.variant_sha256
        or requested_artifact.file_size_bytes != candidate.file_size_bytes
    ):
        return False

    canonical_digest = (canonical_sha256 or "").strip().lower()
    for key in sorted(required_keys):
        artifact = artifacts_by_key[key]
        source_digest = (artifact.source_canonical_sha256 or "").strip().lower()
        content_digest = (artifact.variant_sha256 or "").strip().lower()
        if (
            key not in ALLOWED_VARIANT_KEYS
            or SHA256_PATTERN.fullmatch(source_digest) is None
            or source_digest != canonical_digest
            or SHA256_PATTERN.fullmatch(content_digest) is None
            or not _is_within_media_root(artifact.storage_path, media_root)
        ):
            return False

        artifact_path = Path(artifact.storage_path)
        try:
            expected_size = int(artifact.file_size_bytes)
            if expected_size <= 0 or not file_exists(artifact_path):
                return False
            actual_size = int(file_size(artifact_path))
        except (OSError, TypeError, ValueError):
            return False
        if actual_size != expected_size:
            return False

    return True


def resolve_public_model_path(
    *,
    canonical_path: str,
    canonical_sha256: str | None,
    requested_variant: str | None,
    variant_candidate: PublicVariantCandidate | None,
    variants_enabled: bool,
    media_root: str | None = None,
    file_exists: Callable[[Path], bool] = _default_file_exists,
    file_size: Callable[[Path], int] = _default_file_size,
) -> Path:
    """Default-deny public model resolver.

    Returns the optimized variant path ONLY when every gate passes; otherwise it
    falls back to the canonical model. It never raises, never builds a path from
    the requested token, and never serves a blocked/stale/unapproved/unattributed/
    out-of-root/missing variant. Order is fail-fast to canonical:

    1. no variant requested
    2. requested token is permanently blocked (ios-low-memory)
    3. global kill switch off
    4. token not in the allowlist
    5. no matching candidate row
    6. candidate key mismatch (defensive)
    7. not approved
    8. visual QA not passed
    9. device QA not passed
    10. not flagged public-selectable
    11. four-eyes: approver identity absent (fail closed)
    12. four-eyes: approver identity equals the author (fail closed)
    13. stale: candidate's source canonical SHA != current canonical SHA
    14. variant storage_path does not resolve under media_root
    15. variant file missing on disk
    16. variant has no positive persisted byte size or the file size differs
    17. variant has no valid persisted content digest
    """
    canonical = Path(canonical_path)

    key = normalize_variant_key(requested_variant)
    if not key:
        return canonical
    if key in PERMANENTLY_BLOCKED_VARIANTS:
        return canonical
    if not variants_enabled:
        return canonical
    if key not in ALLOWED_VARIANT_KEYS:
        return canonical
    if variant_candidate is None:
        return canonical
    if normalize_variant_key(variant_candidate.variant_key) != key:
        return canonical
    if variant_candidate.approval_status != APPROVED_STATUS:
        return canonical
    if variant_candidate.visual_qa_status != QA_PASSED_STATUS:
        return canonical
    if variant_candidate.device_qa_status != QA_PASSED_STATUS:
        return canonical
    if not variant_candidate.is_public_selectable:
        return canonical
    # Four-eyes fail-closed: a publicly selectable variant must carry an approver
    # identity that is present and distinct from the author. A null/blank approver
    # is an unattributable approval and must never serve a non-canonical asset.
    approver = (variant_candidate.approved_by or "").strip()
    author = (variant_candidate.created_by or "").strip()
    if not approver:
        return canonical
    if approver == author:
        return canonical
    expected = (variant_candidate.source_canonical_sha256 or "").strip().lower()
    actual = (canonical_sha256 or "").strip().lower()
    if (
        SHA256_PATTERN.fullmatch(expected) is None
        or SHA256_PATTERN.fullmatch(actual) is None
        or expected != actual
    ):
        return canonical
    variant_sha256 = (variant_candidate.variant_sha256 or "").strip().lower()
    if SHA256_PATTERN.fullmatch(variant_sha256) is None:
        return canonical

    # Required object contracts validate every member before either member can
    # resolve. The lookup attaches the fixed pair; malformed/incomplete metadata,
    # peer deletion, path escape, non-regular file, or size drift fails closed.
    if variant_candidate.required_variant_keys or variant_candidate.required_variant_artifacts:
        if not _required_variant_files_are_ready(
            candidate=variant_candidate,
            requested_key=key,
            canonical_sha256=canonical_sha256,
            media_root=media_root,
            file_exists=file_exists,
            file_size=file_size,
        ):
            return canonical
        return Path(variant_candidate.storage_path)

    # Ordinary single-tier confinement: the server-controlled storage path must
    # resolve under the media root. Blocks absolute escapes and ``..`` traversal.
    if not _is_within_media_root(variant_candidate.storage_path, media_root):
        return canonical
    variant_path = Path(variant_candidate.storage_path)
    if not file_exists(variant_path):
        return canonical
    try:
        expected_size = int(variant_candidate.file_size_bytes)
        actual_size = int(file_size(variant_path))
    except (OSError, TypeError, ValueError):
        return canonical
    if expected_size <= 0 or actual_size != expected_size:
        return canonical
    return variant_path


def resolve_public_model_representation(
    *,
    canonical_path: str,
    canonical_sha256: str | None,
    canonical_size_bytes: int | None = None,
    requested_variant: str | None,
    variant_candidate: PublicVariantCandidate | None,
    variants_enabled: bool,
    variant_required: bool = False,
    media_root: str | None = None,
    file_exists: Callable[[Path], bool] = _default_file_exists,
    file_size: Callable[[Path], int] = _default_file_size,
) -> PublicModelRepresentation:
    """Resolve a public model and identify the exact representation served.

    Required-tier requests fail closed when any approval, integrity, confinement,
    or availability gate prevents selection. Ordinary variant requests preserve
    the established canonical fallback contract.
    """

    canonical = Path(canonical_path)
    selected = resolve_public_model_path(
        canonical_path=canonical_path,
        canonical_sha256=canonical_sha256,
        requested_variant=requested_variant,
        variant_candidate=variant_candidate,
        variants_enabled=variants_enabled,
        media_root=media_root,
        file_exists=file_exists,
        file_size=file_size,
    )
    selected_variant = selected != canonical

    if variant_required and not selected_variant:
        return PublicModelRepresentation(
            path=canonical,
            content_sha256=(canonical_sha256 or None),
            expected_size_bytes=canonical_size_bytes,
            is_variant=False,
            required_variant_unavailable=True,
        )

    if selected_variant and variant_candidate is not None:
        return PublicModelRepresentation(
            path=selected,
            content_sha256=(variant_candidate.variant_sha256 or None),
            expected_size_bytes=variant_candidate.file_size_bytes,
            is_variant=True,
        )

    return PublicModelRepresentation(
        path=canonical,
        content_sha256=(canonical_sha256 or None),
        expected_size_bytes=canonical_size_bytes,
        is_variant=False,
    )


def lookup_public_variant_candidate(
    db: Session,
    object_model_id: uuid.UUID,
    variant: str | None,
    *,
    website_object_id: str | None = None,
) -> PublicVariantCandidate | None:
    """Fetch the approved + selectable candidate row for (model, variant_key).

    Returns None for blocked/non-allowlisted keys without touching the DB, and
    only ever selects rows that are already ``approved`` and ``is_public_selectable``
    so the read path can never surface a draft/rejected/revoked variant.
    """
    key = normalize_variant_key(variant)
    if key in PERMANENTLY_BLOCKED_VARIANTS or key not in ALLOWED_VARIANT_KEYS:
        return None

    # Imported here to keep this module import-light and avoid any import-order
    # coupling with the ORM metadata.
    from app.models.entities import ObjectModelDeliveryProfile, ObjectModelVariant

    # ``website_object_id`` remains an ignored compatibility keyword for callers
    # compiled against the frozen R5 boundary. Delivery policy now comes solely
    # from the database profile keyed by the canonical model.
    del website_object_id
    profile = db.execute(
        select(ObjectModelDeliveryProfile).where(
            ObjectModelDeliveryProfile.object_model_id == object_model_id
        )
    ).scalar_one_or_none()
    required_set = _profile_variant_keys(profile)
    candidate_required_keys: frozenset[str] = frozenset()
    required_artifacts: tuple[PublicRequiredVariantArtifact, ...] = ()
    if profile is not None:
        # A configured profile owns its tier tokens even while inactive. This
        # prevents a manually toggled row from bypassing coordinated activation.
        if not profile.is_active or not required_set:
            return None
        if key not in required_set:
            return None
        rows = list(
            db.execute(
                select(ObjectModelVariant).where(
                    ObjectModelVariant.object_model_id == object_model_id,
                    ObjectModelVariant.variant_key.in_(required_set),
                    ObjectModelVariant.approval_status == APPROVED_STATUS,
                    ObjectModelVariant.visual_qa_status == QA_PASSED_STATUS,
                    ObjectModelVariant.device_qa_status == QA_PASSED_STATUS,
                    ObjectModelVariant.is_public_selectable.is_(True),
                )
            ).scalars()
        )
        rows_by_key = {normalize_variant_key(candidate.variant_key): candidate for candidate in rows}
        if rows_by_key.keys() != required_set:
            return None
        row = rows_by_key[key]
        candidate_required_keys = required_set
        required_artifacts = tuple(
            PublicRequiredVariantArtifact(
                variant_key=required_key,
                storage_path=rows_by_key[required_key].storage_path,
                source_canonical_sha256=rows_by_key[required_key].source_canonical_sha256,
                variant_sha256=rows_by_key[required_key].variant_sha256,
                file_size_bytes=rows_by_key[required_key].file_size_bytes,
            )
            for required_key in sorted(required_set)
        )
    else:
        row = db.execute(
            select(ObjectModelVariant).where(
                ObjectModelVariant.object_model_id == object_model_id,
                ObjectModelVariant.variant_key == key,
                ObjectModelVariant.approval_status == APPROVED_STATUS,
                ObjectModelVariant.is_public_selectable.is_(True),
            )
        ).scalar_one_or_none()
    if row is None:
        return None

    return PublicVariantCandidate(
        variant_key=row.variant_key,
        storage_path=row.storage_path,
        approval_status=row.approval_status,
        visual_qa_status=row.visual_qa_status,
        device_qa_status=row.device_qa_status,
        is_public_selectable=bool(row.is_public_selectable),
        source_canonical_sha256=row.source_canonical_sha256,
        variant_sha256=row.variant_sha256,
        file_size_bytes=row.file_size_bytes,
        created_by=str(row.created_by) if row.created_by is not None else None,
        approved_by=str(row.approved_by) if row.approved_by is not None else None,
        required_variant_keys=candidate_required_keys,
        required_variant_artifacts=required_artifacts,
    )
