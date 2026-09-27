"""Secure operator path to publish ONE approved optimized object-model variant.

This is the write side that complements the read-side guard in
``object_model_variants.py``. It is intentionally NOT an HTTP endpoint: it is an
internal command core, invoked by ``app.scripts.publish_object_model_variant``,
so there is no public/admin write surface and no request input ever reaches a
filesystem write.

Security properties enforced here (fail-fast, raising ``VariantPublishError``):
- variant_key must be allowlisted (mobile-1024/2048/4096 or web-8192); ios-low-memory and any
  unknown key are rejected — the poisoned token can never be published.
- the candidate source file must resolve strictly under the approved artifact root.
- the destination is SERVER-COMPUTED from the canonical model id, allowlisted
  variant key, content digest, and a high-entropy publication token. Each publish
  receives a new immutable key, and exclusive filesystem publication prevents
  replacement of an existing artifact.
- ``variant_sha256`` / ``file_size_bytes`` are computed from the bytes actually
  copied to the destination.
- ``source_canonical_sha256`` is verified against the current canonical model row
  (and an operator-supplied expected SHA, when given); a drift/stale canonical is
  rejected.
- four-eyes: created_by and approved_by must both be present, valid, and distinct.
- approved/QA-passed statuses are set ONLY when ``approve=True``; the row stays a
  draft otherwise. ``is_public_selectable`` stays false unless ``selectable=True``
  AND ``approve=True`` (the DB CHECK also enforces selectable => approved+QA).
- the global ``public_model_variants_enabled`` kill switch is NOT touched here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.object_model_variants import (
    ALLOWED_VARIANT_KEYS,
    APPROVED_STATUS,
    QA_PASSED_STATUS,
    PublicVariantCandidate,
    SHA256_PATTERN,
    is_allowed_variant_key,
    is_blocked_variant,
    normalize_variant_key,
    resolve_public_model_path,
)


class VariantPublishError(ValueError):
    """Raised when validation or immutable artifact publication cannot complete."""


@dataclass(frozen=True)
class CanonicalModelRef:
    """The fields of the current canonical ``ObjectModel`` the publisher needs.

    Resolved by the CLI from the DB so the service core stays unit-testable
    without the full object_models/objects/users schema."""

    object_model_id: uuid.UUID
    storage_path: str
    sha256_checksum: str
    revision_number: int
    website_object_id: str | None = None


@dataclass(frozen=True)
class VariantMetrics:
    """Asset metrics captured from the offline optimization/QA reports."""

    max_texture_dimension_px: int
    decoded_texture_ram_bytes: int
    triangle_count: int
    vertex_count: int
    draw_call_count: int | None = None
    geometry_extensions: list[str] = field(default_factory=list)
    generator_version: str = ""
    generator_recipe: dict[str, Any] = field(default_factory=dict)
    visual_qa_report_path: str | None = None
    visual_qa_changed_px_ratio: float | None = None
    device_qa_notes: str | None = None


@dataclass
class PublishResult:
    dry_run: bool
    wrote_file: bool
    inserted_row: bool
    destination_path: str
    variant_sha256: str
    file_size_bytes: int
    row_fields: dict[str, Any]


@dataclass(frozen=True)
class VariantSetActivationResult:
    dry_run: bool
    website_object_id: str
    activated_variant_keys: tuple[str, ...]
    changed_variant_keys: tuple[str, ...]


@dataclass(frozen=True)
class VariantSetDeactivationResult:
    dry_run: bool
    website_object_id: str
    deactivated_variant_keys: tuple[str, ...]
    changed_variant_keys: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryProfileConfigurationResult:
    dry_run: bool
    website_object_id: str
    standard_variant_key: str
    constrained_variant_key: str
    created: bool
    changed: bool


def _sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_under(path: str, root: str, *, label: str) -> Path:
    """Resolve ``path`` and assert it is strictly under ``root``; else raise.

    Both sides are resolved (``strict=False``) so absolute escapes and ``..``
    traversal are rejected regardless of file existence.
    """
    if not root:
        raise VariantPublishError(f"{label}: no root configured")
    resolved_root = Path(root).resolve(strict=False)
    resolved = Path(path).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise VariantPublishError(f"{label}: {path!r} does not resolve under {resolved_root}") from exc
    return resolved


def _validate_identity(value: str | None, *, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise VariantPublishError(f"{label} is required")
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise VariantPublishError(f"{label} must be a UUID, got {value!r}") from exc
    return text


def _destination_path(
    media_root: str,
    object_model_id: uuid.UUID,
    variant_key: str,
    variant_sha256: str,
    publication_token: str,
) -> Path:
    """Build a confined immutable destination key from validated server values."""
    if not media_root:
        raise VariantPublishError("media_root is required")
    if SHA256_PATTERN.fullmatch(variant_sha256) is None:
        raise VariantPublishError("variant_sha256 must be a lowercase SHA-256 digest")
    try:
        normalized_token = uuid.UUID(publication_token).hex
    except (ValueError, AttributeError, TypeError) as exc:
        raise VariantPublishError("publication token must be a UUID") from exc
    root = Path(media_root).resolve(strict=False)
    filename = f"{variant_key}.{variant_sha256}.{normalized_token}.glb"
    dest = (root / "object-models" / str(object_model_id) / "variants" / filename).resolve(strict=False)
    # Defense in depth: the computed path must still be under media_root.
    dest.relative_to(root)
    return dest


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _publish_immutable_candidate(
    *,
    candidate: Path,
    media_root: str,
    object_model_id: uuid.UUID,
    variant_key: str,
    chunk_size: int = 1 << 20,
    token_attempts: int = 8,
) -> tuple[Path, str, int]:
    """Copy, hash, and exclusively link one immutable artifact.

    The copy is first written to a unique exclusive temporary file in the final
    directory. ``os.link`` publishes it without overwrite semantics. A final-key
    collision receives a fresh high-entropy token while preserving the existing
    artifact. Every failure before final publication cleans the temporary copy.
    """
    if not media_root:
        raise VariantPublishError("media_root is required")
    root = Path(media_root).resolve(strict=False)
    destination_dir = (root / "object-models" / str(object_model_id) / "variants").resolve(strict=False)
    destination_dir.relative_to(root)
    destination_dir.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    output = None
    for _ in range(token_attempts):
        temporary_token = uuid.uuid4().hex
        candidate_temporary = destination_dir / f".{variant_key}.{temporary_token}.publishing"
        try:
            output = open(candidate_temporary, "xb")
        except FileExistsError:
            continue
        temporary_path = candidate_temporary
        break
    if temporary_path is None or output is None:
        raise VariantPublishError("could not allocate a unique temporary publication file")

    digest = hashlib.sha256()
    file_size = 0
    try:
        with output, open(candidate, "rb") as source:
            for block in iter(lambda: source.read(chunk_size), b""):
                output.write(block)
                digest.update(block)
                file_size += len(block)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        _unlink_best_effort(temporary_path)
        raise

    variant_sha256 = digest.hexdigest()
    for _ in range(token_attempts):
        publication_token = str(uuid.uuid4())
        destination = _destination_path(
            media_root,
            object_model_id,
            variant_key,
            variant_sha256,
            publication_token,
        )
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            continue
        except Exception:
            _unlink_best_effort(temporary_path)
            raise
        _unlink_best_effort(temporary_path)
        return destination, variant_sha256, file_size

    _unlink_best_effort(temporary_path)
    raise VariantPublishError("could not allocate a unique immutable publication key")


def publish_object_model_variant(
    db: Session,
    *,
    canonical: CanonicalModelRef,
    variant_key: str,
    candidate_path: str,
    media_root: str,
    artifact_root: str,
    created_by: str,
    approved_by: str,
    metrics: VariantMetrics,
    expected_canonical_sha256: str | None = None,
    approve: bool = False,
    selectable: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PublishResult:
    from app.models.entities import ObjectModelDeliveryProfile, ObjectModelVariant

    key = normalize_variant_key(variant_key)
    if is_blocked_variant(key) or not is_allowed_variant_key(key):
        raise VariantPublishError(f"variant_key {variant_key!r} is not an allowlisted optimized variant")

    # Four-eyes: both identities present, valid, and distinct.
    author = _validate_identity(created_by, label="created_by")
    approver = _validate_identity(approved_by, label="approved_by")
    if author == approver:
        raise VariantPublishError("approved_by must differ from created_by (four-eyes)")

    if selectable and not approve:
        raise VariantPublishError("selectable requires --approve (approval + QA gates must be satisfied)")

    profile = db.execute(
        select(ObjectModelDeliveryProfile).where(
            ObjectModelDeliveryProfile.object_model_id == canonical.object_model_id
        )
    ).scalar_one_or_none()
    profile_keys = (
        {
            normalize_variant_key(profile.standard_variant_key),
            normalize_variant_key(profile.constrained_variant_key),
        }
        if profile is not None
        else set()
    )
    if selectable and key in profile_keys:
        raise VariantPublishError(
            "profile delivery tiers must be approved as non-selectable rows and activated together"
        )

    # Candidate source confinement: must live under the approved artifact root.
    candidate_resolved = _resolve_under(candidate_path, artifact_root, label="candidate")
    if not candidate_resolved.is_file():
        raise VariantPublishError(f"candidate file not found: {candidate_resolved}")
    if candidate_resolved.suffix.lower() != ".glb":
        raise VariantPublishError(f"candidate must be a .glb file: {candidate_resolved.name}")

    # Canonical SHA verification (stale / drift rejection).
    current_canonical_sha = (canonical.sha256_checksum or "").strip().lower()
    if SHA256_PATTERN.fullmatch(current_canonical_sha) is None:
        raise VariantPublishError("canonical model has no valid recorded sha256_checksum")
    if expected_canonical_sha256 is not None:
        expected = expected_canonical_sha256.strip().lower()
        if expected != current_canonical_sha:
            raise VariantPublishError(
                "stale canonical: expected_canonical_sha256 does not match the current canonical model"
            )
    # When the canonical file is present, verify its bytes match the recorded SHA.
    canonical_on_disk = Path(canonical.storage_path)
    if canonical_on_disk.is_file():
        disk_sha = _sha256_file(canonical_on_disk).lower()
        if disk_sha != current_canonical_sha:
            raise VariantPublishError("canonical model bytes on disk do not match the recorded sha256_checksum")

    # Uniqueness: one variant per (model, key).
    existing = db.execute(
        select(ObjectModelVariant).where(
            ObjectModelVariant.object_model_id == canonical.object_model_id,
            ObjectModelVariant.variant_key == key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise VariantPublishError(f"variant {key} already exists for this object model")

    stamp = now or datetime.now(timezone.utc)
    approval_status = APPROVED_STATUS if approve else "draft"
    qa_status = QA_PASSED_STATUS if approve else "pending"

    row_fields: dict[str, Any] = {
        "object_model_id": canonical.object_model_id,
        "variant_key": key,
        "original_filename": candidate_resolved.name,
        "mime_type": "model/gltf-binary",
        "source_canonical_sha256": current_canonical_sha,
        "source_revision_number": canonical.revision_number,
        "max_texture_dimension_px": metrics.max_texture_dimension_px,
        "decoded_texture_ram_bytes": metrics.decoded_texture_ram_bytes,
        "triangle_count": metrics.triangle_count,
        "vertex_count": metrics.vertex_count,
        "draw_call_count": metrics.draw_call_count,
        "geometry_extensions": list(metrics.geometry_extensions or []),
        "generator_version": metrics.generator_version,
        "generator_recipe": dict(metrics.generator_recipe or {}),
        "visual_qa_status": qa_status,
        "visual_qa_changed_px_ratio": metrics.visual_qa_changed_px_ratio,
        "visual_qa_report_path": metrics.visual_qa_report_path,
        "device_qa_status": qa_status,
        "device_qa_notes": metrics.device_qa_notes,
        "approval_status": approval_status,
        "created_by": uuid.UUID(author),
        "is_public_selectable": bool(selectable and approve),
    }
    if approve:
        approver_uuid = uuid.UUID(approver)
        row_fields.update(
            {
                "submitted_by": uuid.UUID(author),
                "submitted_at": stamp,
                "approved_by": approver_uuid,
                "approved_at": stamp,
                "visual_qa_by": approver_uuid,
                "visual_qa_at": stamp,
                "device_qa_by": approver_uuid,
                "device_qa_at": stamp,
            }
        )

    if dry_run:
        # Preview only: nothing copied, nothing inserted. variant_sha256/size are
        # previewed from the candidate. The displayed destination is representative;
        # a real publication always receives a fresh high-entropy token.
        preview_sha = _sha256_file(candidate_resolved)
        preview_size = candidate_resolved.stat().st_size
        preview_dest = _destination_path(
            media_root,
            canonical.object_model_id,
            key,
            preview_sha,
            str(uuid.uuid4()),
        )
        return PublishResult(
            dry_run=True,
            wrote_file=False,
            inserted_row=False,
            destination_path=str(preview_dest),
            variant_sha256=preview_sha,
            file_size_bytes=preview_size,
            row_fields={
                **row_fields,
                "storage_path": str(preview_dest),
                "variant_sha256": preview_sha,
                "file_size_bytes": preview_size,
            },
        )

    # Copy once into a unique immutable key using exclusive filesystem publication.
    dest, variant_sha, file_size = _publish_immutable_candidate(
        candidate=candidate_resolved,
        media_root=media_root,
        object_model_id=canonical.object_model_id,
        variant_key=key,
    )
    row_fields["storage_path"] = str(dest)
    row_fields["variant_sha256"] = variant_sha
    row_fields["file_size_bytes"] = file_size

    commit_attempted = False
    try:
        row = ObjectModelVariant(**row_fields)
        db.add(row)
        # Once this call begins, its outcome can be ambiguous to the caller. Retain
        # this publication's unique artifact for a gated orphan collector to assess.
        commit_attempted = True
        db.commit()
    except Exception:
        try:
            db.rollback()
        finally:
            if not commit_attempted:
                _unlink_best_effort(dest)
        raise

    return PublishResult(
        dry_run=False,
        wrote_file=True,
        inserted_row=True,
        destination_path=str(dest),
        variant_sha256=variant_sha,
        file_size_bytes=file_size,
        row_fields=row_fields,
    )


def _delivery_profile(db: Session, object_model_id: uuid.UUID, *, lock: bool):
    from app.models.entities import ObjectModelDeliveryProfile

    stmt = select(ObjectModelDeliveryProfile).where(
        ObjectModelDeliveryProfile.object_model_id == object_model_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _profile_keys(profile) -> frozenset[str]:
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


def _audit_delivery_change(
    db: Session,
    *,
    event_type: str,
    canonical: CanonicalModelRef,
    actor_id: uuid.UUID,
    profile,
    payload: dict[str, Any],
) -> None:
    from app.models.entities import AuditEvent

    db.add(
        AuditEvent(
            event_type=event_type,
            subject_type="object_model_delivery_profile",
            subject_id=canonical.object_model_id,
            root_public_id=(canonical.website_object_id or "").strip() or None,
            actor_json={"user_id": str(actor_id)},
            payload_json={
                "profile_id": str(profile.id),
                "object_model_id": str(canonical.object_model_id),
                "standard_variant_key": profile.standard_variant_key,
                "constrained_variant_key": profile.constrained_variant_key,
                **payload,
            },
        )
    )


def configure_object_delivery_profile(
    db: Session,
    *,
    canonical: CanonicalModelRef,
    website_object_id: str,
    standard_variant_key: str,
    constrained_variant_key: str,
    actor_id: str,
    dry_run: bool = False,
) -> DeliveryProfileConfigurationResult:
    """Create or update one inactive data-driven delivery profile."""

    from app.models.entities import ObjectModelDeliveryProfile

    object_id = (website_object_id or "").strip().lower()
    if canonical.website_object_id and canonical.website_object_id.strip().lower() != object_id:
        raise VariantPublishError("canonical model does not belong to the requested public object")
    standard_key = normalize_variant_key(standard_variant_key)
    constrained_key = normalize_variant_key(constrained_variant_key)
    if not is_allowed_variant_key(standard_key) or not is_allowed_variant_key(constrained_key):
        raise VariantPublishError("delivery profile contains a non-allowlisted variant token")
    if standard_key == constrained_key:
        raise VariantPublishError("standard and constrained delivery tiers must differ")
    actor_uuid = uuid.UUID(_validate_identity(actor_id, label="actor_id"))

    profile = _delivery_profile(db, canonical.object_model_id, lock=True)
    created = profile is None
    if profile is not None and profile.is_active:
        db.rollback()
        raise VariantPublishError("deactivate the delivery profile before changing its tier mapping")
    before = (
        None
        if profile is None
        else {
            "standard_variant_key": profile.standard_variant_key,
            "constrained_variant_key": profile.constrained_variant_key,
        }
    )
    changed = before != {
        "standard_variant_key": standard_key,
        "constrained_variant_key": constrained_key,
    }
    if profile is None:
        profile = ObjectModelDeliveryProfile(
            object_model_id=canonical.object_model_id,
            standard_variant_key=standard_key,
            constrained_variant_key=constrained_key,
            created_by=actor_uuid,
        )
        db.add(profile)
        db.flush()
    else:
        profile.standard_variant_key = standard_key
        profile.constrained_variant_key = constrained_key

    result = DeliveryProfileConfigurationResult(
        dry_run=dry_run,
        website_object_id=object_id,
        standard_variant_key=standard_key,
        constrained_variant_key=constrained_key,
        created=created,
        changed=changed,
    )
    if dry_run:
        db.rollback()
        return result
    _audit_delivery_change(
        db,
        event_type="object_model_delivery_profile_configured",
        canonical=canonical,
        actor_id=actor_uuid,
        profile=profile,
        payload={"before": before, "after": {"standard": standard_key, "constrained": constrained_key}},
    )
    db.commit()
    return result


def activate_required_object_variant_set(
    db: Session,
    *,
    canonical: CanonicalModelRef,
    website_object_id: str,
    media_root: str,
    actor_id: str | None = None,
    dry_run: bool = False,
) -> VariantSetActivationResult:
    """Atomically activate the configured tier set for one object profile."""

    from app.models.entities import ObjectModelVariant

    object_id = (website_object_id or "").strip().lower()
    if canonical.website_object_id and canonical.website_object_id.strip().lower() != object_id:
        raise VariantPublishError("canonical model does not belong to the requested public object")
    profile = _delivery_profile(db, canonical.object_model_id, lock=True)
    required_set = _profile_keys(profile)
    if not required_set:
        db.rollback()
        raise VariantPublishError("object has no valid delivery profile")
    actor_uuid = uuid.UUID(
        _validate_identity(actor_id or str(profile.created_by), label="actor_id")
    )

    rows = list(
        db.execute(
            select(ObjectModelVariant)
            .where(
                ObjectModelVariant.object_model_id == canonical.object_model_id,
                ObjectModelVariant.variant_key.in_(required_set),
            )
            .with_for_update()
        ).scalars()
    )
    rows_by_key = {normalize_variant_key(row.variant_key): row for row in rows}
    missing = sorted(required_set - rows_by_key.keys())
    if missing:
        db.rollback()
        raise VariantPublishError(f"required delivery tier set is incomplete: missing {', '.join(missing)}")

    for key in sorted(required_set):
        row = rows_by_key[key]
        candidate = PublicVariantCandidate(
            variant_key=row.variant_key,
            storage_path=row.storage_path,
            approval_status=row.approval_status,
            visual_qa_status=row.visual_qa_status,
            device_qa_status=row.device_qa_status,
            is_public_selectable=True,
            source_canonical_sha256=row.source_canonical_sha256,
            variant_sha256=row.variant_sha256,
            file_size_bytes=row.file_size_bytes,
            created_by=str(row.created_by) if row.created_by is not None else None,
            approved_by=str(row.approved_by) if row.approved_by is not None else None,
        )
        try:
            variant_path = _resolve_under(row.storage_path, media_root, label=f"required delivery tier {key}")
            persisted_size = int(row.file_size_bytes)
            file_stat = variant_path.stat()
        except VariantPublishError:
            db.rollback()
            raise
        except FileNotFoundError as exc:
            db.rollback()
            raise VariantPublishError(f"required delivery tier file is absent: {key}") from exc
        except (OSError, TypeError, ValueError) as exc:
            db.rollback()
            raise VariantPublishError(f"required delivery tier size could not be verified: {key}") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            db.rollback()
            raise VariantPublishError(f"required delivery tier file is absent: {key}")
        actual_size = file_stat.st_size
        if persisted_size <= 0 or actual_size != persisted_size:
            db.rollback()
            raise VariantPublishError(f"required delivery tier size mismatch: {key}")
        resolved = resolve_public_model_path(
            canonical_path=canonical.storage_path,
            canonical_sha256=canonical.sha256_checksum,
            requested_variant=key,
            variant_candidate=candidate,
            variants_enabled=True,
            media_root=media_root,
            file_exists=lambda path, expected=variant_path: path == expected,
            file_size=lambda _path, size=actual_size: size,
        )
        if resolved == Path(canonical.storage_path):
            db.rollback()
            raise VariantPublishError(f"required delivery tier failed activation gates: {key}")
        try:
            actual_sha256 = _sha256_file(variant_path)
        except OSError as exc:
            db.rollback()
            raise VariantPublishError(f"required delivery tier digest could not be verified: {key}") from exc
        if not hmac.compare_digest(actual_sha256, (row.variant_sha256 or "").strip().lower()):
            db.rollback()
            raise VariantPublishError(f"required delivery tier content digest mismatch: {key}")

    changed = tuple(sorted(key for key, row in rows_by_key.items() if not row.is_public_selectable))
    result = VariantSetActivationResult(
        dry_run=dry_run,
        website_object_id=object_id,
        activated_variant_keys=tuple(sorted(required_set)),
        changed_variant_keys=changed,
    )
    if dry_run:
        db.rollback()
        return result
    stamp = datetime.now(timezone.utc)
    try:
        for row in rows_by_key.values():
            row.is_public_selectable = True
        profile.is_active = True
        profile.activated_by = actor_uuid
        profile.activated_at = stamp
        profile.deactivated_by = None
        profile.deactivated_at = None
        _audit_delivery_change(
            db,
            event_type="object_model_delivery_profile_activated",
            canonical=canonical,
            actor_id=actor_uuid,
            profile=profile,
            payload={
                "changed_variant_keys": list(changed),
                "variants": [
                    {
                        "variant_key": key,
                        "variant_id": str(rows_by_key[key].id),
                        "sha256": rows_by_key[key].variant_sha256,
                        "bytes": rows_by_key[key].file_size_bytes,
                        "source_canonical_sha256": rows_by_key[key].source_canonical_sha256,
                    }
                    for key in sorted(required_set)
                ],
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def deactivate_required_object_variant_set(
    db: Session,
    *,
    canonical: CanonicalModelRef,
    website_object_id: str,
    actor_id: str | None = None,
    dry_run: bool = False,
) -> VariantSetDeactivationResult:
    """Atomically contain one profile, including when peer artifacts are absent."""

    from app.models.entities import ObjectModelVariant

    object_id = (website_object_id or "").strip().lower()
    if canonical.website_object_id and canonical.website_object_id.strip().lower() != object_id:
        raise VariantPublishError("canonical model does not belong to the requested public object")
    profile = _delivery_profile(db, canonical.object_model_id, lock=True)
    required_set = _profile_keys(profile)
    if not required_set:
        db.rollback()
        raise VariantPublishError("object has no valid delivery profile")
    actor_uuid = uuid.UUID(
        _validate_identity(actor_id or str(profile.created_by), label="actor_id")
    )
    rows = list(
        db.execute(
            select(ObjectModelVariant)
            .where(
                ObjectModelVariant.object_model_id == canonical.object_model_id,
                ObjectModelVariant.variant_key.in_(required_set),
            )
            .with_for_update()
        ).scalars()
    )
    rows_by_key = {normalize_variant_key(row.variant_key): row for row in rows}
    changed = tuple(sorted(key for key, row in rows_by_key.items() if row.is_public_selectable))
    present = tuple(sorted(rows_by_key))
    result = VariantSetDeactivationResult(
        dry_run=dry_run,
        website_object_id=object_id,
        deactivated_variant_keys=present,
        changed_variant_keys=changed,
    )
    if dry_run:
        db.rollback()
        return result
    stamp = datetime.now(timezone.utc)
    try:
        for row in rows_by_key.values():
            row.is_public_selectable = False
        profile.is_active = False
        profile.deactivated_by = actor_uuid
        profile.deactivated_at = stamp
        _audit_delivery_change(
            db,
            event_type="object_model_delivery_profile_deactivated",
            canonical=canonical,
            actor_id=actor_uuid,
            profile=profile,
            payload={"changed_variant_keys": list(changed), "present_variant_keys": list(present)},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
