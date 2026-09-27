"""Exact-approval compare-and-swap activation for validated playback artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.models.entities import Video, VideoStatus
from app.services.media_transcode import (
    PUBLIC_PLAYBACK_RECIPE_REVISION,
    PlaybackArtifactValidation,
    transcode_output_dir,
    versioned_transcode_output_path,
)
from app.services.public_media_paths import resolve_public_media_file

SELECTOR_FIELDS = (
    "playback_sha256_checksum",
    "playback_file_size_bytes",
    "playback_recipe_version",
    "playback_color_policy",
    "playback_storage_key",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COLOR_POLICIES = {
    "sdr-bt709-preserve",
    "sdr-bt709-full-to-limited",
    "sdr-bt2020-to-bt709",
}


class PlaybackPromotionRejected(RuntimeError):
    """The approved artifact or current row state failed closed validation."""


def _selector_state(video: Video) -> dict[str, object]:
    return {field: getattr(video, field) for field in SELECTOR_FIELDS}


def _checked_video(db, video_id: str) -> Video:
    if db.new or db.dirty or db.deleted:
        raise PlaybackPromotionRejected("session_has_pending_changes")
    try:
        parsed_id = uuid.UUID(str(video_id))
    except (ValueError, TypeError, AttributeError):
        raise PlaybackPromotionRejected("invalid_video_id") from None
    statement = (
        select(Video)
        .where(Video.id == parsed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    video = db.execute(statement).scalar_one_or_none()
    if video is None:
        db.rollback()
        raise PlaybackPromotionRejected("video_not_found")
    if getattr(video.status, "value", video.status) != VideoStatus.READY.value:
        db.rollback()
        raise PlaybackPromotionRejected("video_not_ready")
    return video


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_approved_candidate(
    candidate_path: str | Path,
    artifact: PlaybackArtifactValidation,
) -> tuple[Path, Path]:
    candidate = resolve_public_media_file(candidate_path)
    if candidate is None:
        raise PlaybackPromotionRejected("candidate_outside_media_root_or_missing")
    if artifact.recipe_revision != PUBLIC_PLAYBACK_RECIPE_REVISION:
        raise PlaybackPromotionRejected("unknown_playback_recipe")
    if artifact.color_policy not in _COLOR_POLICIES:
        raise PlaybackPromotionRejected("unknown_playback_color_policy")
    if not _SHA256.fullmatch(artifact.sha256) or artifact.size_bytes <= 0:
        raise PlaybackPromotionRejected("invalid_artifact_identity")
    actual_digest, actual_size = _digest_file(candidate)
    if actual_digest != artifact.sha256 or actual_size != artifact.size_bytes:
        raise PlaybackPromotionRejected("candidate_identity_mismatch")

    media_root = Path(settings.media_root).resolve(strict=True)
    destination_root = transcode_output_dir().resolve()
    try:
        destination_root.relative_to(media_root)
    except ValueError:
        raise PlaybackPromotionRejected("transcode_directory_outside_media_root") from None
    return candidate, destination_root


def _validate_before_state(expected_before: dict[str, object]) -> dict[str, object]:
    if not isinstance(expected_before, dict) or set(expected_before) != set(SELECTOR_FIELDS):
        raise PlaybackPromotionRejected("expected_selector_state_incomplete")
    return dict(expected_before)


def promote_validated_playback(
    db,
    *,
    video_id: str,
    expected_source_sha256: str,
    expected_before: dict[str, object],
    candidate_path: str | Path,
    artifact: PlaybackArtifactValidation,
    publication_token: str | None = None,
) -> dict[str, object]:
    """Select a validator-produced artifact against a locked approved snapshot.

    Trusted operators use a dedicated clean Session and keep the returned
    receipt in their private release journal. Artifact validation and human
    approval precede this call. This function creates no HTTP write surface.
    """
    before_approved = _validate_before_state(expected_before)
    if not isinstance(expected_source_sha256, str) or not _SHA256.fullmatch(expected_source_sha256):
        raise PlaybackPromotionRejected("invalid_expected_source_sha256")
    if not isinstance(artifact, PlaybackArtifactValidation):
        raise PlaybackPromotionRejected("validated_playback_artifact_required")
    candidate, _destination_root = _validate_approved_candidate(candidate_path, artifact)
    token = publication_token or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise PlaybackPromotionRejected("invalid_publication_token")

    video = _checked_video(db, video_id)
    try:
        if video.sha256_checksum != expected_source_sha256:
            raise PlaybackPromotionRejected("source_checksum_changed")
        before = _selector_state(video)
        if before != before_approved:
            raise PlaybackPromotionRejected("playback_selector_changed")

        output_path = versioned_transcode_output_path(str(video.id), artifact.sha256, token)
        if output_path.parent.resolve() != _destination_root:
            raise PlaybackPromotionRejected("invalid_destination_path")
    except BaseException:
        db.rollback()
        raise
    created_output = False
    commit_attempted = False
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a fresh immutable name, then hash the actual copied bytes
        # before touching the row. This closes the candidate check/copy race.
        with candidate.open("rb") as source, output_path.open("xb") as destination:
            created_output = True
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        copied_digest, copied_size = _digest_file(output_path)
        if copied_digest != artifact.sha256 or copied_size != artifact.size_bytes:
            raise PlaybackPromotionRejected("copied_candidate_identity_mismatch")
        output_path.chmod(0o444)
        # Persist the new directory entry before a durable DB row can select it.
        directory_fd = os.open(output_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        video.playback_sha256_checksum = artifact.sha256
        video.playback_file_size_bytes = artifact.size_bytes
        video.playback_recipe_version = artifact.recipe_revision
        video.playback_color_policy = artifact.color_policy
        video.playback_storage_key = output_path.name
        commit_attempted = True
        db.commit()
    except BaseException:
        db.rollback()
        # Preserve a file if commit outcome is ambiguous; a committed row may
        # already select it. Pre-commit failures only remove our new file.
        if created_output and not commit_attempted:
            output_path.unlink(missing_ok=True)
        raise

    after = _selector_state(video)
    return {
        "video_id": str(video.id),
        "source_sha256": expected_source_sha256,
        "before": before,
        "after": after,
    }


def rollback_validated_playback(db, receipt: dict[str, object]) -> dict[str, object]:
    """Restore a promotion's prior selector only while its after-state is current."""
    if not isinstance(receipt, dict):
        raise PlaybackPromotionRejected("invalid_promotion_receipt")
    try:
        video_id = str(receipt["video_id"])
        source_sha256 = str(receipt["source_sha256"])
        before = _validate_before_state(receipt["before"])
        after = _validate_before_state(receipt["after"])
    except (KeyError, TypeError):
        raise PlaybackPromotionRejected("invalid_promotion_receipt") from None
    if not _SHA256.fullmatch(source_sha256):
        raise PlaybackPromotionRejected("invalid_promotion_receipt")

    video = _checked_video(db, video_id)
    if video.sha256_checksum != source_sha256:
        db.rollback()
        raise PlaybackPromotionRejected("source_checksum_changed")
    if _selector_state(video) != after:
        db.rollback()
        raise PlaybackPromotionRejected("playback_selector_changed_since_promotion")

    try:
        for field, value in before.items():
            setattr(video, field, value)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {
        "video_id": str(video.id),
        "source_sha256": source_sha256,
        "before": after,
        "after": before,
    }
