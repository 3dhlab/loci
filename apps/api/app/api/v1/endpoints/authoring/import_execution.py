"""Stage B5 — real, DB-backed guarded import execution.

This is the only module in the authoring lane that mutates the public
``title_card_observations`` table. It is invoked exclusively by the guarded
import route AFTER every preflight/readiness/four-eyes gate has passed and the
server has recomputed and matched the fixture SHA. It never calls OCR/OpenAI,
never reaches a remote host, and confines all file I/O to the approved
artifact/backup roots.

Each helper is small and side-effect-explicit so the orchestration in
``routes.py`` reads as a linear checklist and the dangerous step (the row write)
is isolated and easy to audit.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.authoring.imports import DEFAULT_BACKUP_ROOT, compute_sha256, confine_import_path
from app.models.entities import TitleCardObservation, Video
from app.services.title_card_ocr import (
    TitleCardImportBatch,
    import_title_card_observation_batch,
)


def resolve_target_video(db: Session, production_video_uuid: uuid.UUID | None) -> Video | None:
    """The production ``videos`` row the import would attach observations to."""
    if production_video_uuid is None:
        return None
    return db.get(Video, production_video_uuid)


def count_target_source_version_rows(db: Session, video_id: uuid.UUID, source_version: str) -> int:
    """Existing ``title_card_observations`` rows for (video_id, source_version)."""
    return db.execute(
        select(func.count())
        .select_from(TitleCardObservation)
        .where(
            TitleCardObservation.video_id == video_id,
            TitleCardObservation.source_version == source_version,
        )
    ).scalar_one()


def capture_preimport_backup(
    db: Session,
    *,
    video_id: uuid.UUID,
    stamp: str,
    backup_root: str | None = None,
) -> dict[str, Any]:
    """Dump all existing title-card rows for ``video_id`` to a confined JSON file.

    Always writes a file (even for an empty set — the empty-state snapshot is the
    authoritative rollback baseline, mirroring the proven manual lane). Returns
    the backup path, its SHA-256, and the row count. The path is confined to the
    backup root so a crafted ``video_id``/stamp cannot escape it.
    """
    root = Path(backup_root or DEFAULT_BACKUP_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    filename = f"title-card-observations.video-{video_id}.preimport-{stamp}.json"
    backup_path = confine_import_path(filename, root)

    rows = db.execute(
        select(TitleCardObservation).where(TitleCardObservation.video_id == video_id)
    ).scalars().all()
    payload = [
        {
            "id": str(r.id),
            "timestamp_ms": r.timestamp_ms,
            "prompt_version": r.prompt_version,
            "source_kind": r.source_kind,
            "source_version": r.source_version,
            "public_speaker_label": r.public_speaker_label,
            "session_date_text": r.session_date_text,
        }
        for r in rows
    ]
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "path": str(backup_path),
        "sha256": compute_sha256(backup_path),
        "row_count": len(payload),
    }


def perform_guarded_import(
    db: Session,
    *,
    video: Video,
    batch: TitleCardImportBatch,
    expected_source_version: str,
) -> dict[str, Any]:
    """Write observation rows from an already-parsed, SHA-verified batch.

    The caller hashes the fixture bytes and parses the SAME bytes into ``batch``
    (single-read hardening), so there is no reopen between hashing and import.
    Uses ``--replace-matching-source`` semantics (prune only rows whose prompt
    version + source kind + source version match the imported batch). Refuses if
    the batch's declared source_version does not equal the approved one, so the
    bytes that were reviewed are exactly the bytes imported.
    """
    if batch.source_version != expected_source_version:
        raise ValueError(
            f"fixture source_version {batch.source_version!r} != approved {expected_source_version!r}"
        )
    summary = import_title_card_observation_batch(
        db, video=video, batch=batch, model_run_id=None, replace_existing=True
    )
    return {
        "status": "completed",
        "inserted": summary.get("inserted", 0),
        "updated": summary.get("updated", 0),
        "removed": summary.get("removed", 0),
        "observations_written": summary.get("observations_written", 0),
        "fixture_observation_count": len(batch.observations),
    }


def perform_scoped_rollback(db: Session, *, video_id: uuid.UUID, source_version: str) -> int:
    """Delete ONLY rows matching (video_id, source_version). Returns the count.

    This is the exact, minimal rollback scope — no other source version and no
    other video is touched.
    """
    result = db.execute(
        delete(TitleCardObservation).where(
            TitleCardObservation.video_id == video_id,
            TitleCardObservation.source_version == source_version,
        )
    )
    return int(result.rowcount or 0)
