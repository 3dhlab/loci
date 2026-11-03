from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.entities import (
    AnnotationReviewStatus,
    Clip,
    ClipStatus,
    CorpusPhrase,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    Project,
    Segment,
    Transcript,
    TranscriptFormat,
    TranscriptSource,
    TranscriptWindow,
    User,
    Video,
    VideoStatus,
    VisualWindowDescription,
)
from app.services.publication_media_inventory import MediaInventoryError, build_media_inventory
from app.services.media_transcode import transcode_output_dir
from app.services.publication_attribution import validate_public_attribution_bands

PUBLIC_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
PUBLIC_OWNER_EMAIL = "public-projection@semantic.local"
PUBLIC_OWNER_PASSWORD = "public-projection-disabled"


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _ensure_public_owner(db) -> User:
    user = db.get(User, PUBLIC_OWNER_ID)
    if user is None:
        user = User(
            id=PUBLIC_OWNER_ID,
            email=PUBLIC_OWNER_EMAIL,
            password_hash=get_password_hash(PUBLIC_OWNER_PASSWORD),
            is_active=False,
        )
    else:
        user.email = PUBLIC_OWNER_EMAIL
        user.is_active = False
    db.add(user)
    db.flush()
    return user


def _public_media_path(relative_path: str) -> str:
    return str((Path(settings.media_root).resolve() / relative_path).resolve())


def _public_media_path_or_none(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    return _public_media_path(relative_path)


def _legacy_playback_identity(video_id: uuid.UUID, relative_path: str) -> dict | None:
    """Retain a versioned rendition's identity when using the legacy importer.

    The preservation/upload checksum in the manifest is not the normalized MP4
    checksum. Read only the canonical rendition path, bind its filename digest
    to its actual regular-file bytes, and retain the storage key the playback
    resolver expects. Fixed legacy paths keep their existing nullable identity.
    No generation identity is inferred from a path without its commitment.
    """
    try:
        directory = transcode_output_dir().relative_to(Path(settings.media_root).resolve())
        if any(part == ".." or "\\" in part or any(ord(c) < 32 or ord(c) == 127 for c in part)
               for part in directory.parts):
            raise ValueError()
        prefix = "" if directory == Path(".") else directory.as_posix() + "/"
    except (TypeError, ValueError):
        raise ValueError("Legacy playback directory must stay inside the configured media root.") from None
    fixed = f"{prefix}{video_id}.mp4"
    if relative_path == fixed:
        return None
    if not isinstance(relative_path, str) or not relative_path.startswith(prefix):
        raise ValueError("Legacy playback must use its configured rendition directory.")
    name = relative_path[len(prefix):]
    match = re.fullmatch(rf"{re.escape(str(video_id))}\.([0-9a-f]{{64}})\.[0-9a-f]{{32}}\.mp4", name)
    if match is None:
        raise ValueError("Legacy versioned playback identity is invalid.")
    try:
        identity = build_media_inventory(settings.media_root, [relative_path])[relative_path]
    except MediaInventoryError:
        raise ValueError("Legacy versioned playback is unavailable or changed.") from None
    if identity["sha256"] != match.group(1):
        raise ValueError("Legacy versioned playback bytes differ from their named identity.")
    return {"storage_key": name, "sha256": identity["sha256"], "size_bytes": identity["size_bytes"]}


def _rehydrate_visual_frame_manifest(frame_manifest: dict | None) -> dict | None:
    if not isinstance(frame_manifest, dict):
        return frame_manifest

    hydrated = json.loads(json.dumps(frame_manifest))

    thumbnail = hydrated.get("thumbnail")
    if isinstance(thumbnail, dict):
        thumbnail["path"] = _public_media_path_or_none(thumbnail.get("path"))

    for entry in hydrated.get("sample_frames") or []:
        if not isinstance(entry, dict):
            continue
        entry["path"] = _public_media_path_or_none(entry.get("path"))

    return hydrated


def _delete_missing(db, model, ids: set[uuid.UUID]) -> None:
    if ids:
        db.execute(delete(model).where(model.id.notin_(ids)))
    else:
        db.execute(delete(model))


def _prepare_ordinal_replacement(db, model, attribute: str, rows: list[dict], ids: set[uuid.UUID]) -> None:
    """Free obsolete/swap keys without deleting retained row identities.

    These immediate PostgreSQL uniques cannot accept a newly ingested UUID at
    an old ordinal, or a swap of two retained ordinals. Prune only obsolete
    children, then park retained values at unused signed-integer keys before
    assigning final values. All writes stay in the caller's transaction. The
    temporary keys avoid BOTH existing and selected keys, including negative
    values, and synchronize any ORM instances already loaded by the caller.
    """
    if not rows:
        return
    _delete_missing(db, model, ids)
    column = getattr(model, attribute)
    current = db.execute(select(model.id, column)).all()
    reserved = {int(value) for _, value in current} | {int(row[attribute]) for row in rows}
    temporary = -1
    assignments = []
    for row_id, _ in current:
        while temporary in reserved and temporary >= -(2**31):
            temporary -= 1
        if temporary < -(2**31):
            raise ValueError("No safe temporary transcript ordinal is available.")
        assignments.append((row_id, temporary))
        reserved.add(temporary)
        temporary -= 1
    for row_id, value in assignments:
        db.execute(update(model).where(model.id == row_id).values({attribute: value})
                   .execution_options(synchronize_session="fetch"))


def _reject_retained_reparenting(db, model, parent_attribute: str, rows: list[dict]) -> None:
    """Refuse moving a retained child before pruning could cascade through it."""
    if not rows:
        return
    selected = {uuid.UUID(row["id"]): uuid.UUID(row[parent_attribute]) for row in rows}
    current = db.execute(select(model.id, getattr(model, parent_attribute))
                         .where(model.id.in_(selected))).all()
    if any(selected[row_id] != parent_id for row_id, parent_id in current):
        raise ValueError("A retained transcript child cannot change its parent in this projection.")


def import_projection(payload: dict, db, *, generation_id=None, playback_inventory=None) -> dict:
    """Apply projection rows in the caller's transaction; never commit here."""
    frozen_attribution = {}
    for row in payload["videos"]:
        if generation_id or "publication_attribution_json" in row:
            frozen_attribution[row["id"]] = validate_public_attribution_bands(row.get("publication_attribution_json"))
    project_ids = {uuid.UUID(row["id"]) for row in payload["projects"]}
    object_ids = {uuid.UUID(row["id"]) for row in payload["objects"]}
    video_ids = {uuid.UUID(row["id"]) for row in payload["videos"]}
    transcript_ids = {uuid.UUID(row["id"]) for row in payload["transcripts"]}
    segment_ids = {uuid.UUID(row["id"]) for row in payload["segments"]}
    window_ids = {uuid.UUID(row["id"]) for row in payload["transcript_windows"]}
    visual_window_ids = {uuid.UUID(row["id"]) for row in payload.get("visual_window_descriptions", [])}
    corpus_phrase_ids = {uuid.UUID(row["id"]) for row in payload.get("corpus_phrases", [])}
    model_ids = {uuid.UUID(row["id"]) for row in payload["object_models"]}
    clip_ids = {uuid.UUID(row["id"]) for row in payload.get("clips", [])}
    annotation_ids = {uuid.UUID(row["id"]) for row in payload["object_model_annotations"]}

    if payload["segments"]:
        _reject_retained_reparenting(db, Segment, "transcript_id", payload["segments"])
    if payload["transcript_windows"]:
        _reject_retained_reparenting(db, TranscriptWindow, "transcript_id", payload["transcript_windows"])
    if payload.get("visual_window_descriptions"):
        _reject_retained_reparenting(db, VisualWindowDescription, "transcript_window_id",
                                    payload["visual_window_descriptions"])
    _ensure_public_owner(db)

    # Re-ingest keeps the Transcript identity but can replace/reorder its child
    # UUIDs. Release old immediate-unique keys before any new-row flush; retained
    # annotation/clip identities remain in place and are rebound below. Window
    # deletion may cascade only its obsolete visual children in this same tx.
    if payload["transcript_windows"]:
        _delete_missing(db, VisualWindowDescription, visual_window_ids)
    if payload["segments"]:
        _prepare_ordinal_replacement(db, Segment, "position", payload["segments"], segment_ids)
    if payload["transcript_windows"]:
        _prepare_ordinal_replacement(db, TranscriptWindow, "window_index", payload["transcript_windows"], window_ids)

    for row in payload["projects"]:
        row_id = uuid.UUID(row["id"])
        project = db.get(Project, row_id) or Project(id=row_id, owner_id=PUBLIC_OWNER_ID, name=row["name"])
        project.owner_id = PUBLIC_OWNER_ID
        project.name = row["name"]
        project.description = row.get("description")
        project.website_slug = row.get("website_slug")
        project.created_at = _parse_dt(row.get("created_at")) or project.created_at
        project.updated_at = _parse_dt(row.get("updated_at")) or project.updated_at
        db.add(project)

    for row in payload["objects"]:
        row_id = uuid.UUID(row["id"])
        obj = db.get(Object, row_id) or Object(id=row_id, project_id=uuid.UUID(row["project_id"]), name=row["name"])
        obj.project_id = uuid.UUID(row["project_id"])
        obj.name = row["name"]
        obj.description = row.get("description")
        obj.external_url = row.get("external_url")
        obj.website_object_id = row.get("website_object_id")
        obj.metadata_json = row.get("metadata_json")
        obj.is_published = bool(row.get("is_published", True))
        obj.is_embed_ready = bool(row.get("is_embed_ready", False))
        obj.created_at = _parse_dt(row.get("created_at")) or obj.created_at
        obj.updated_at = _parse_dt(row.get("updated_at")) or obj.updated_at
        db.add(obj)

    for row in payload["videos"]:
        row_id = uuid.UUID(row["id"])
        video = db.get(Video, row_id) or Video(
            id=row_id,
            project_id=uuid.UUID(row["project_id"]),
            object_id=uuid.UUID(row["object_id"]) if row.get("object_id") else None,
            stable_video_id=row["stable_video_id"],
            title=row["title"],
            original_filename=row["original_filename"],
            source_path=_public_media_path(row["public_media_relpath"]),
            sha256_checksum=row["sha256_checksum"],
        )
        video.project_id = uuid.UUID(row["project_id"])
        video.object_id = uuid.UUID(row["object_id"]) if row.get("object_id") else None
        video.stable_video_id = row["stable_video_id"]
        video.title = row["title"]
        video.original_filename = row["original_filename"]
        video.source_path = _public_media_path(row["public_media_relpath"])
        video.publication_generation_id = uuid.UUID(generation_id) if generation_id else None
        video.publication_attribution_json = frozen_attribution.get(row["id"])
        playback = (playback_inventory or {}).get(row["id"])
        if generation_id and playback is None:
            raise ValueError("The selected generation has no playback byte identity.")
        video.playback_storage_key = None
        video.playback_sha256_checksum = playback["sha256"] if playback else None
        video.playback_file_size_bytes = playback["size_bytes"] if playback else None
        video.playback_recipe_version = "publication-generation-v1" if generation_id else None
        video.playback_color_policy = None
        if not generation_id:
            legacy_playback = _legacy_playback_identity(row_id, row["public_media_relpath"])
            if legacy_playback is not None:
                video.playback_storage_key = legacy_playback["storage_key"]
                video.playback_sha256_checksum = legacy_playback["sha256"]
                video.playback_file_size_bytes = legacy_playback["size_bytes"]
        video.sha256_checksum = row["sha256_checksum"]
        video.duration_ms = row.get("duration_ms")
        video.status = VideoStatus(row["status"])
        video.transcode_progress_pct = 100 if video.status == VideoStatus.READY else None
        video.transcode_stage = "ready" if video.status == VideoStatus.READY else None
        video.transcode_started_at = None
        video.is_published = bool(row.get("is_published", True))
        video.created_at = _parse_dt(row.get("created_at")) or video.created_at
        video.updated_at = _parse_dt(row.get("updated_at")) or video.updated_at
        db.add(video)

    for row in payload["transcripts"]:
        row_id = uuid.UUID(row["id"])
        transcript = db.get(Transcript, row_id) or Transcript(
            id=row_id,
            video_id=uuid.UUID(row["video_id"]),
            title=row["title"],
            source=TranscriptSource(row["source"]),
            language=row["language"],
            format=TranscriptFormat(row["format"]),
            raw_text=row["raw_text"],
        )
        transcript.video_id = uuid.UUID(row["video_id"])
        transcript.title = row["title"]
        transcript.source = TranscriptSource(row["source"])
        transcript.language = row["language"]
        transcript.format = TranscriptFormat(row["format"])
        transcript.raw_text = row["raw_text"]
        transcript.is_published = bool(row.get("is_published", True))
        transcript.created_at = _parse_dt(row.get("created_at")) or transcript.created_at
        transcript.updated_at = _parse_dt(row.get("updated_at")) or transcript.updated_at
        db.add(transcript)

    for row in payload["segments"]:
        row_id = uuid.UUID(row["id"])
        segment = db.get(Segment, row_id) or Segment(
            id=row_id,
            transcript_id=uuid.UUID(row["transcript_id"]),
            position=int(row["position"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=row["text"],
        )
        segment.transcript_id = uuid.UUID(row["transcript_id"])
        segment.position = int(row["position"])
        segment.start_ms = int(row["start_ms"])
        segment.end_ms = int(row["end_ms"])
        segment.text = row["text"]
        segment.embedding_vector = row.get("embedding_vector")
        segment.created_at = _parse_dt(row.get("created_at")) or segment.created_at
        db.add(segment)

    for row in payload["transcript_windows"]:
        row_id = uuid.UUID(row["id"])
        window = db.get(TranscriptWindow, row_id) or TranscriptWindow(
            id=row_id,
            transcript_id=uuid.UUID(row["transcript_id"]),
            window_index=int(row["window_index"]),
            start_position=int(row["start_position"]),
            end_position=int(row["end_position"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=row["text"],
        )
        window.transcript_id = uuid.UUID(row["transcript_id"])
        window.window_index = int(row["window_index"])
        window.start_position = int(row["start_position"])
        window.end_position = int(row["end_position"])
        window.start_ms = int(row["start_ms"])
        window.end_ms = int(row["end_ms"])
        window.text = row["text"]
        window.embedding_vector = row.get("embedding_vector")
        window.created_at = _parse_dt(row.get("created_at")) or window.created_at
        db.add(window)

    # Flush transcript_windows first so FK guard against actual DB state works.
    # This forces SQLAlchemy to insert/update transcript_windows BEFORE any
    # visual_window_description that references them.
    db.flush()

    # Re-query the public DB for the set of transcript_window_ids that actually
    # exist NOW (after the flush above). This is the authoritative set; we use
    # it to filter visual_descriptions defensively, regardless of whether the
    # export manifest is internally consistent.
    from sqlalchemy import select as _select  # local alias to avoid shadowing
    existing_window_ids = {
        row_id for (row_id,) in db.execute(_select(TranscriptWindow.id)).all()
    }

    skipped_visual_orphans = 0
    for row in payload.get("visual_window_descriptions", []):
        row_id = uuid.UUID(row["id"])
        transcript_window_id = uuid.UUID(row["transcript_window_id"])
        # Defensive guard against actual DB state: skip visual_descriptions whose
        # parent transcript_window is NOT in the public DB after the flush above.
        # Prevents FK violations from any manifest inconsistency or export quirk.
        if transcript_window_id not in existing_window_ids:
            skipped_visual_orphans += 1
            visual_window_ids.discard(row_id)
            continue
        visual_row = db.get(VisualWindowDescription, row_id) or VisualWindowDescription(
            id=row_id,
            transcript_window_id=transcript_window_id,
            transcript_id=uuid.UUID(row["transcript_id"]),
            video_id=uuid.UUID(row["video_id"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            status=row.get("status") or "pending",
        )
        visual_row.transcript_window_id = uuid.UUID(row["transcript_window_id"])
        visual_row.transcript_id = uuid.UUID(row["transcript_id"])
        visual_row.video_id = uuid.UUID(row["video_id"])
        visual_row.start_ms = int(row["start_ms"])
        visual_row.end_ms = int(row["end_ms"])
        visual_row.description_text = row.get("description_text")
        visual_row.embedding_vector = row.get("embedding_vector")
        visual_row.thumbnail_path = _public_media_path_or_none(row.get("thumbnail_relpath"))
        visual_row.frame_manifest_json = _rehydrate_visual_frame_manifest(row.get("frame_manifest_json"))
        visual_row.generator_provider = row.get("generator_provider")
        visual_row.generator_model = row.get("generator_model")
        visual_row.embedding_provider = row.get("embedding_provider")
        visual_row.embedding_model = row.get("embedding_model")
        visual_row.status = row.get("status") or "pending"
        visual_row.created_at = _parse_dt(row.get("created_at")) or visual_row.created_at
        visual_row.updated_at = _parse_dt(row.get("updated_at")) or visual_row.updated_at
        db.add(visual_row)

    for row in payload.get("corpus_phrases", []):
        row_id = uuid.UUID(row["id"])
        corpus_phrase = db.get(CorpusPhrase, row_id) or CorpusPhrase(
            id=row_id,
            project_id=uuid.UUID(row["project_id"]),
            phrase_text=row["phrase_text"],
            normalized_phrase=row["normalized_phrase"],
            source=row["source"],
            source_count=int(row.get("source_count") or 1),
            corpus_density=row.get("corpus_density") or 0,
        )
        corpus_phrase.project_id = uuid.UUID(row["project_id"])
        corpus_phrase.phrase_text = row["phrase_text"]
        corpus_phrase.normalized_phrase = row["normalized_phrase"]
        corpus_phrase.embedding_vector = row.get("embedding_vector")
        corpus_phrase.source = row["source"]
        corpus_phrase.source_count = int(row.get("source_count") or 1)
        corpus_phrase.corpus_density = row.get("corpus_density") or 0
        corpus_phrase.created_at = _parse_dt(row.get("created_at")) or corpus_phrase.created_at
        corpus_phrase.updated_at = _parse_dt(row.get("updated_at")) or corpus_phrase.updated_at
        db.add(corpus_phrase)

    for row in payload["object_models"]:
        row_id = uuid.UUID(row["id"])
        model = db.get(ObjectModel, row_id) or ObjectModel(
            id=row_id,
            object_id=uuid.UUID(row["object_id"]),
            storage_path=_public_media_path(row["public_media_relpath"]),
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            sha256_checksum=row["sha256_checksum"],
            file_size_bytes=int(row["file_size_bytes"]),
            revision_number=int(row["revision_number"]),
            uploaded_by=PUBLIC_OWNER_ID,
        )
        model.object_id = uuid.UUID(row["object_id"])
        model.storage_path = _public_media_path(row["public_media_relpath"])
        model.original_filename = row["original_filename"]
        model.mime_type = row["mime_type"]
        model.sha256_checksum = row["sha256_checksum"]
        model.file_size_bytes = int(row["file_size_bytes"])
        model.revision_number = int(row["revision_number"])
        model.position_x = row["position_x"]
        model.position_y = row["position_y"]
        model.position_z = row["position_z"]
        model.rotation_x = row["rotation_x"]
        model.rotation_y = row["rotation_y"]
        model.rotation_z = row["rotation_z"]
        model.default_camera_json = row.get("default_camera_json")
        model.public_poster_path = _public_media_path(row["public_poster_relpath"]) if row.get("public_poster_relpath") else None
        model.is_published = bool(row.get("is_published", True))
        model.uploaded_by = PUBLIC_OWNER_ID
        model.created_at = _parse_dt(row.get("created_at")) or model.created_at
        model.updated_at = _parse_dt(row.get("updated_at")) or model.updated_at
        db.add(model)

    for row in payload.get("clips", []):
        row_id = uuid.UUID(row["id"])
        clip = db.get(Clip, row_id) or Clip(
            id=row_id,
            video_id=uuid.UUID(row["video_id"]),
            project_id=uuid.UUID(row["project_id"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
        )
        clip.video_id = uuid.UUID(row["video_id"])
        clip.project_id = uuid.UUID(row["project_id"])
        clip.start_ms = int(row["start_ms"])
        clip.end_ms = int(row["end_ms"])
        clip.output_mp4_path = _public_media_path(row["public_media_relpath"])
        clip.metadata_json_path = _public_media_path(row["public_transcript_relpath"]) if row.get("public_transcript_relpath") else None
        clip.website_clip_id = row.get("website_clip_id")
        clip.transcript_excerpt = row.get("transcript_excerpt")
        clip.citation_text = row.get("citation_text")
        clip.status = ClipStatus(row.get("status") or ClipStatus.COMPLETE.value)
        clip.created_at = _parse_dt(row.get("created_at")) or clip.created_at
        clip.updated_at = _parse_dt(row.get("updated_at")) or clip.updated_at
        db.add(clip)

    for row in payload["object_model_annotations"]:
        row_id = uuid.UUID(row["id"])
        annotation = db.get(ObjectModelAnnotation, row_id) or ObjectModelAnnotation(
            id=row_id,
            object_model_id=uuid.UUID(row["object_model_id"]),
            object_id=uuid.UUID(row["object_id"]),
            video_id=uuid.UUID(row["video_id"]),
            clip_id=uuid.UUID(row["clip_id"]) if row.get("clip_id") else None,
            transcript_segment_id=uuid.UUID(row["transcript_segment_id"]) if row.get("transcript_segment_id") else None,
            title=row["title"],
            description=row.get("description"),
            point_x=row["point_x"],
            point_y=row["point_y"],
            point_z=row["point_z"],
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            review_status=AnnotationReviewStatus(row["review_status"]),
            model_revision_created_against=int(row["model_revision_created_against"]),
            created_by=PUBLIC_OWNER_ID,
        )
        annotation.object_model_id = uuid.UUID(row["object_model_id"])
        annotation.object_id = uuid.UUID(row["object_id"])
        annotation.video_id = uuid.UUID(row["video_id"])
        annotation.clip_id = uuid.UUID(row["clip_id"]) if row.get("clip_id") else None
        annotation.transcript_segment_id = (
            uuid.UUID(row["transcript_segment_id"]) if row.get("transcript_segment_id") else None
        )
        annotation.title = row["title"]
        annotation.description = row.get("description")
        annotation.point_x = row["point_x"]
        annotation.point_y = row["point_y"]
        annotation.point_z = row["point_z"]
        annotation.normal_x = row.get("normal_x")
        annotation.normal_y = row.get("normal_y")
        annotation.normal_z = row.get("normal_z")
        annotation.camera_json = row.get("camera_json")
        annotation.playlist_json = row.get("playlist_json")
        annotation.website_annotation_id = row.get("website_annotation_id")
        annotation.website_relations_json = row.get("website_relations_json") or {
            "publication_ids": [],
            "project_ids": [],
            "location_ids": [],
        }
        annotation.start_ms = int(row["start_ms"])
        annotation.end_ms = int(row["end_ms"])
        annotation.review_status = AnnotationReviewStatus(row["review_status"])
        annotation.is_published = bool(row.get("is_published", True))
        annotation.model_revision_created_against = int(row["model_revision_created_against"])
        annotation.created_by = PUBLIC_OWNER_ID
        annotation.created_at = _parse_dt(row.get("created_at")) or annotation.created_at
        annotation.updated_at = _parse_dt(row.get("updated_at")) or annotation.updated_at
        db.add(annotation)

    if skipped_visual_orphans:
        print(
            f"[import] skipped {skipped_visual_orphans} orphan visual_window_descriptions "
            "(transcript_window_id not in this projection batch)",
            file=sys.stderr,
        )

    db.flush()

    _delete_missing(db, ObjectModelAnnotation, annotation_ids)
    _delete_missing(db, CorpusPhrase, corpus_phrase_ids)
    _delete_missing(db, VisualWindowDescription, visual_window_ids)
    _delete_missing(db, TranscriptWindow, window_ids)
    _delete_missing(db, Segment, segment_ids)
    _delete_missing(db, ObjectModel, model_ids)
    _delete_missing(db, Clip, clip_ids)
    _delete_missing(db, Transcript, transcript_ids)
    _delete_missing(db, Video, video_ids)
    _delete_missing(db, Object, object_ids)
    _delete_missing(db, Project, project_ids)


    return {"project_count": len(project_ids), "object_count": len(object_ids),
            "video_count": len(video_ids), "transcript_count": len(transcript_ids),
            "segment_count": len(segment_ids), "transcript_window_count": len(window_ids),
            "visual_window_description_count": len(visual_window_ids),
            "corpus_phrase_count": len(corpus_phrase_ids), "model_count": len(model_ids),
            "clip_count": len(clip_ids), "annotation_count": len(annotation_ids),
            "public_owner_id": str(PUBLIC_OWNER_ID)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a published object-package projection into semantic_public.")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    from sqlalchemy import text
    from app.models.publication_job import PublicationDestinationHead
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x4c4f43495055424c})
        summary = import_projection(payload, db)
        # A legacy import has no durable-generation commitment. Invalidate any
        # previously verified head in the same transaction as its row changes.
        current = db.get(PublicationDestinationHead, "global")
        if current is None:
            current = PublicationDestinationHead(lane="global", generation_id=None, version=1)
            db.add(current)
        else:
            current.generation_id = None
            current.version += 1
            db.add(current)
        db.commit()
    print(json.dumps({"manifest": args.manifest, **summary}, indent=2))


if __name__ == "__main__":
    main()
