from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Clip, Object, ObjectModel, ObjectModelAnnotation, Project, PublicationManifest, Transcript, TranscriptSource, Video, VideoStatus
from app.services.audit_events import append_audit_event
from app.services.media_clips import clip_poster_path_for_video
from app.services.media_transcode import transcode_output_path
from app.services.publication_manifest import PublicationManifestBuildInput, PublicationManifestFocusV1, PublicationManifestV1, build_publication_manifest


LOCAL_SOURCE_ENVIRONMENT = "semantic_local"
PUBLIC_DESTINATION_ENVIRONMENT = "semantic_public"


@dataclass(slots=True)
class PersistedPublicationManifestResult:
    current_manifest_ids: list[UUID]
    created_count: int
    reused_count: int
    superseded_count: int


@dataclass(slots=True)
class _PublicationManifestCandidate:
    schema_version: int
    package_type: str
    root_entity_type: str
    root_entity_id: UUID
    root_public_id: str
    project_id: UUID
    project_public_slug: str | None
    canonical_path: str
    public_title: str | None
    public_summary: str | None
    public_payload_json: dict
    internal_linkage_json: dict
    media_manifest_json: dict
    projection_digest: str
    source_environment: str
    destination_environment: str
    source_release_tag: str | None
    actor_json: dict
    generated_at: datetime


def persist_publication_manifests(
    db: Session,
    *,
    actor_json: dict | None,
    generated_at: str | None,
    source_release_tag: str | None = None,
    source_environment: str = LOCAL_SOURCE_ENVIRONMENT,
    destination_environment: str = PUBLIC_DESTINATION_ENVIRONMENT,
) -> PersistedPublicationManifestResult:
    normalized_actor_json = _normalized_actor_json(actor_json)
    normalized_generated_at = _parse_timestamp(generated_at)
    candidates = _build_manifest_candidates(
        db,
        actor_json=normalized_actor_json,
        generated_at=normalized_generated_at,
        source_release_tag=source_release_tag,
        source_environment=source_environment,
        destination_environment=destination_environment,
    )

    created_count = 0
    reused_count = 0
    superseded_count = 0
    current_manifest_ids: list[UUID] = []

    for candidate in candidates:
        current_row = db.execute(
            select(PublicationManifest).where(
                PublicationManifest.package_type == candidate.package_type,
                PublicationManifest.root_public_id == candidate.root_public_id,
                PublicationManifest.is_current.is_(True),
            )
        ).scalar_one_or_none()

        if current_row is not None and current_row.projection_digest == candidate.projection_digest:
            current_manifest_ids.append(current_row.id)
            reused_count += 1
            continue

        if current_row is not None:
            current_row.is_current = False
            current_row.superseded_at = candidate.generated_at
            db.add(current_row)
            append_audit_event(
                db,
                event_type="publication_manifest.superseded",
                subject_type="publication_manifest",
                subject_id=current_row.id,
                publication_manifest_id=current_row.id,
                root_public_id=current_row.root_public_id,
                actor_json=candidate.actor_json,
                payload_json={
                    "previous_projection_digest": current_row.projection_digest,
                    "next_projection_digest": candidate.projection_digest,
                },
                created_at=candidate.generated_at,
            )
            superseded_count += 1

        next_row = PublicationManifest(
            id=uuid4(),
            schema_version=candidate.schema_version,
            package_type=candidate.package_type,
            root_entity_type=candidate.root_entity_type,
            root_entity_id=candidate.root_entity_id,
            root_public_id=candidate.root_public_id,
            project_id=candidate.project_id,
            project_public_slug=candidate.project_public_slug,
            canonical_path=candidate.canonical_path,
            public_title=candidate.public_title,
            public_summary=candidate.public_summary,
            public_payload_json=candidate.public_payload_json,
            internal_linkage_json=candidate.internal_linkage_json,
            media_manifest_json=candidate.media_manifest_json,
            projection_digest=candidate.projection_digest,
            source_environment=candidate.source_environment,
            destination_environment=candidate.destination_environment,
            source_release_tag=candidate.source_release_tag,
            actor_json=candidate.actor_json,
            generated_at=candidate.generated_at,
            projected_at=None,
            superseded_at=None,
            is_current=True,
        )
        db.add(next_row)
        append_audit_event(
            db,
            event_type="publication_manifest.created",
            subject_type="publication_manifest",
            subject_id=next_row.id,
            publication_manifest_id=next_row.id,
            root_public_id=next_row.root_public_id,
            actor_json=candidate.actor_json,
            payload_json={
                "root_entity_id": next_row.root_entity_id,
                "project_id": next_row.project_id,
                "canonical_path": next_row.canonical_path,
                "projection_digest": next_row.projection_digest,
                "is_current": True,
            },
            created_at=candidate.generated_at,
        )
        current_manifest_ids.append(next_row.id)
        created_count += 1

    db.commit()

    return PersistedPublicationManifestResult(
        current_manifest_ids=current_manifest_ids,
        created_count=created_count,
        reused_count=reused_count,
        superseded_count=superseded_count,
    )


def mark_publication_manifests_projected(
    db: Session,
    manifest_ids: list[UUID],
    *,
    projected_at: str | datetime | None = None,
    actor_json: dict | None = None,
) -> int:
    if not manifest_ids:
        return 0

    normalized_projected_at = _parse_timestamp(projected_at)
    rows = db.execute(
        select(PublicationManifest).where(PublicationManifest.id.in_(manifest_ids))
    ).scalars().all()

    updated_count = 0
    for row in rows:
        if row.projected_at is not None:
            continue
        row.projected_at = normalized_projected_at
        db.add(row)
        append_audit_event(
            db,
            event_type="publication_manifest.projected",
            subject_type="publication_manifest",
            subject_id=row.id,
            publication_manifest_id=row.id,
            root_public_id=row.root_public_id,
            actor_json=actor_json,
            payload_json={"projected_at": normalized_projected_at},
            created_at=normalized_projected_at,
        )
        updated_count += 1

    if updated_count:
        db.commit()

    return updated_count


def _build_manifest_candidates(
    db: Session,
    *,
    actor_json: dict,
    generated_at: datetime,
    source_release_tag: str | None,
    source_environment: str,
    destination_environment: str,
) -> list[_PublicationManifestCandidate]:
    object_rows = db.execute(
        select(Object)
        .where(Object.is_published.is_(True), Object.is_embed_ready.is_(True))
        .order_by(Object.created_at.asc(), Object.id.asc())
    ).scalars().all()
    if not object_rows:
        return []

    object_ids = [row.id for row in object_rows]
    project_rows = db.execute(
        select(Project).where(Project.id.in_({row.project_id for row in object_rows}))
    ).scalars().all()
    project_by_id = {row.id: row for row in project_rows}

    model_rows = db.execute(
        select(ObjectModel)
        .where(ObjectModel.object_id.in_(object_ids), ObjectModel.is_published.is_(True))
        .order_by(ObjectModel.created_at.asc(), ObjectModel.id.asc())
    ).scalars().all()
    model_by_object_id = {row.object_id: row for row in model_rows}

    video_rows = db.execute(
        select(Video)
        .where(Video.object_id.in_(object_ids), Video.is_published.is_(True), Video.status == VideoStatus.READY)
        .order_by(Video.created_at.asc(), Video.id.asc())
    ).scalars().all()
    videos_by_object_id: dict[UUID, list[Video]] = defaultdict(list)
    for video in video_rows:
        if video.object_id is None:
            continue
        videos_by_object_id[video.object_id].append(video)

    video_ids = [row.id for row in video_rows]
    clip_rows = db.execute(
        select(Clip)
        .where(Clip.video_id.in_(video_ids) if video_ids else False)
        .order_by(Clip.created_at.asc(), Clip.id.asc())
    ).scalars().all() if video_ids else []
    clips_by_object_id: dict[UUID, list[Clip]] = defaultdict(list)
    video_object_id_map = {video.id: video.object_id for video in video_rows if video.object_id is not None}
    for clip in clip_rows:
        object_id = video_object_id_map.get(clip.video_id)
        if object_id is not None:
            clips_by_object_id[object_id].append(clip)

    annotation_rows = db.execute(
        select(ObjectModelAnnotation)
        .where(ObjectModelAnnotation.object_id.in_(object_ids), ObjectModelAnnotation.is_published.is_(True))
        .order_by(ObjectModelAnnotation.created_at.asc(), ObjectModelAnnotation.id.asc())
    ).scalars().all()
    annotations_by_object_id: dict[UUID, list[ObjectModelAnnotation]] = defaultdict(list)
    for annotation in annotation_rows:
        annotations_by_object_id[annotation.object_id].append(annotation)

    transcript_rows = db.execute(
        select(Transcript)
        .where(Transcript.video_id.in_(video_ids) if video_ids else False, Transcript.is_published.is_(True))
        .order_by(Transcript.updated_at.desc(), Transcript.created_at.desc(), Transcript.id.asc())
    ).scalars().all() if video_ids else []
    transcripts_by_video_id: dict[UUID, list[Transcript]] = defaultdict(list)
    for transcript in transcript_rows:
        transcripts_by_video_id[transcript.video_id].append(transcript)

    candidates: list[_PublicationManifestCandidate] = []
    for object_row in object_rows:
        project = project_by_id.get(object_row.project_id)
        if project is None:
            continue

        model = model_by_object_id.get(object_row.id)
        videos = videos_by_object_id.get(object_row.id, [])
        annotations = annotations_by_object_id.get(object_row.id, [])
        clips = clips_by_object_id.get(object_row.id, [])
        runtime_manifest = build_publication_manifest(
            PublicationManifestBuildInput(
                object_row=object_row,
                model=model,
                videos=videos,
                annotations=annotations,
                clips=clips,
            )
        )
        if runtime_manifest is None:
            continue

        public_title = _optional_nonempty_string(object_row.name)
        public_summary = _public_summary(object_row)
        public_payload_json = _public_payload(runtime_manifest, project=project, public_title=public_title, public_summary=public_summary)
        internal_linkage_json = _internal_linkage_payload(
            runtime_manifest,
            object_row=object_row,
            model=model,
            project=project,
            videos=videos,
            transcripts_by_video_id=transcripts_by_video_id,
        )
        media_manifest_json = _media_manifest_payload(object_row=object_row, model=model, videos=videos, clips=clips)
        projection_digest = _stable_package_digest(
            {
                "schema_version": runtime_manifest.schema_version,
                "package_type": runtime_manifest.package_type,
                "root_entity_type": "object",
                "root_entity_id": str(object_row.id),
                "root_public_id": runtime_manifest.object_entry.public_id,
                "project_id": str(object_row.project_id),
                "project_public_slug": _optional_nonempty_string(project.website_slug),
                "canonical_path": runtime_manifest.object_entry.evidence_url,
                "public_title": public_title,
                "public_summary": public_summary,
                "public_payload_json": public_payload_json,
                "internal_linkage_json": internal_linkage_json,
                "media_manifest_json": media_manifest_json,
                "source_environment": source_environment,
                "destination_environment": destination_environment,
            }
        )

        candidates.append(
            _PublicationManifestCandidate(
                schema_version=runtime_manifest.schema_version,
                package_type=runtime_manifest.package_type,
                root_entity_type="object",
                root_entity_id=object_row.id,
                root_public_id=runtime_manifest.object_entry.public_id,
                project_id=object_row.project_id,
                project_public_slug=_optional_nonempty_string(project.website_slug),
                canonical_path=runtime_manifest.object_entry.evidence_url,
                public_title=public_title,
                public_summary=public_summary,
                public_payload_json=public_payload_json,
                internal_linkage_json=internal_linkage_json,
                media_manifest_json=media_manifest_json,
                projection_digest=projection_digest,
                source_environment=source_environment,
                destination_environment=destination_environment,
                source_release_tag=source_release_tag,
                actor_json=actor_json,
                generated_at=generated_at,
            )
        )

    return candidates


def _public_payload(
    runtime_manifest: PublicationManifestV1,
    *,
    project: Project,
    public_title: str | None,
    public_summary: str | None,
) -> dict:
    return {
        "schemaVersion": runtime_manifest.schema_version,
        "packageType": runtime_manifest.package_type,
        "root": {
            "entityType": "object",
            "publicId": runtime_manifest.object_entry.public_id,
            "canonicalPath": runtime_manifest.object_entry.evidence_url,
        },
        "project": {
            "slug": _optional_nonempty_string(project.website_slug),
            "name": _optional_nonempty_string(project.name),
        },
        "publication": {
            "title": public_title,
            "summary": public_summary,
        },
        "defaultFocus": _focus_payload(runtime_manifest.default_focus),
        "included": {
            "annotationIds": sorted(runtime_manifest.annotations_by_public_id.keys()),
            "clipIds": sorted(runtime_manifest.clips_by_public_id.keys()),
            "videoIds": sorted(runtime_manifest.videos_by_public_id.keys()),
        },
    }


def _internal_linkage_payload(
    runtime_manifest: PublicationManifestV1,
    *,
    object_row: Object,
    model: ObjectModel | None,
    project: Project,
    videos: list[Video],
    transcripts_by_video_id: dict[UUID, list[Transcript]],
) -> dict:
    videos_by_public_id = {video.stable_video_id: video for video in videos if _optional_nonempty_string(video.stable_video_id)}
    transcripts = []
    for video_public_id in sorted(runtime_manifest.videos_by_public_id.keys()):
        video = videos_by_public_id.get(video_public_id)
        if video is None:
            continue
        preferred_transcript = _preferred_published_transcript(transcripts_by_video_id.get(video.id, []))
        if preferred_transcript is None:
            continue
        transcripts.append(
            {
                "internalId": str(preferred_transcript.id),
                "videoInternalId": str(video.id),
                "videoPublicId": video_public_id,
                "publicLocator": f"video:{video_public_id}:preferred_transcript",
            }
        )

    return {
        "root": {
            "entityType": "object",
            "internalId": str(object_row.id),
            "publicId": runtime_manifest.object_entry.public_id,
        },
        "project": {
            "internalId": str(project.id),
            "publicSlug": _optional_nonempty_string(project.website_slug),
        },
        "model": (
            {
                "internalId": str(model.id),
                "publicLocator": _public_model_locator(object_row.id, model),
            }
            if model is not None
            else None
        ),
        "annotations": [
            {
                "internalId": str(entry.internal_id),
                "publicId": entry.public_id,
                "videoPublicId": entry.video_public_id,
            }
            for entry in sorted(runtime_manifest.annotations_by_public_id.values(), key=lambda item: item.public_id)
        ],
        "clips": [
            {
                "internalId": str(entry.internal_id),
                "publicId": entry.public_id,
                "videoPublicId": entry.video_public_id,
            }
            for entry in sorted(runtime_manifest.clips_by_public_id.values(), key=lambda item: item.public_id)
        ],
        "videos": [
            {
                "internalId": str(entry.internal_id),
                "publicId": entry.public_id,
                "streamLocator": _video_stream_locator(entry.internal_id),
            }
            for entry in sorted(runtime_manifest.videos_by_public_id.values(), key=lambda item: item.public_id)
        ],
        "transcripts": transcripts,
    }


def _media_manifest_payload(*, object_row: Object, model: ObjectModel | None, videos: list[Video], clips: list[Clip]) -> dict:
    items: list[dict] = []

    if model is not None:
        model_path = _existing_file(_normalized_path(model.storage_path))
        if model_path is not None:
            items.append(
                _media_item(
                    kind="model",
                    path=model_path,
                    owner_entity_type="object_model",
                    owner_internal_id=model.id,
                    owner_public_id=object_row.website_object_id,
                    extra={"mimeType": model.mime_type},
                )
            )
        poster_path = _existing_file(_normalized_path(model.public_poster_path))
        if poster_path is not None:
            items.append(
                _media_item(
                    kind="model_poster",
                    path=poster_path,
                    owner_entity_type="object_model",
                    owner_internal_id=model.id,
                    owner_public_id=object_row.website_object_id,
                    extra={"mimeType": "image/svg+xml" if poster_path.suffix.lower() == ".svg" else None},
                )
            )

    for video in sorted(videos, key=lambda item: item.stable_video_id):
        playback_path = transcode_output_path(str(video.id))
        if playback_path.exists() and playback_path.is_file():
            items.append(
                _media_item(
                    kind="video_stream",
                    path=playback_path,
                    owner_entity_type="video",
                    owner_internal_id=video.id,
                    owner_public_id=video.stable_video_id,
                    extra={"mimeType": "video/mp4"},
                )
            )

    for clip in sorted(clips, key=lambda item: (_optional_nonempty_string(item.website_clip_id) or "", item.id)):
        clip_path = _existing_file(_normalized_path(clip.output_mp4_path))
        if clip_path is not None:
            items.append(
                _media_item(
                    kind="clip_video",
                    path=clip_path,
                    owner_entity_type="clip",
                    owner_internal_id=clip.id,
                    owner_public_id=_optional_nonempty_string(clip.website_clip_id),
                    extra={"mimeType": "video/mp4"},
                )
            )
        poster_path = clip_poster_path_for_video(clip.output_mp4_path or "")
        if poster_path.exists() and poster_path.is_file():
            items.append(
                _media_item(
                    kind="clip_poster",
                    path=poster_path,
                    owner_entity_type="clip",
                    owner_internal_id=clip.id,
                    owner_public_id=_optional_nonempty_string(clip.website_clip_id),
                    extra={"mimeType": "image/jpeg"},
                )
            )
        transcript_path = _existing_file(_normalized_path(clip.metadata_json_path))
        if transcript_path is not None:
            items.append(
                _media_item(
                    kind="clip_transcript",
                    path=transcript_path,
                    owner_entity_type="clip",
                    owner_internal_id=clip.id,
                    owner_public_id=_optional_nonempty_string(clip.website_clip_id),
                    extra={"mimeType": "text/plain"},
                )
            )

    return {"items": items}


def _media_item(
    *,
    kind: str,
    path: Path,
    owner_entity_type: str,
    owner_internal_id: UUID,
    owner_public_id: str | None,
    extra: dict | None = None,
) -> dict:
    payload = {
        "kind": kind,
        "relpath": _relative_media_path(path),
        "checksum": f"sha256:{_file_sha256(path)}",
        "sizeBytes": int(path.stat().st_size),
        "owner": {
            "entityType": owner_entity_type,
            "internalId": str(owner_internal_id),
            "publicId": owner_public_id,
        },
    }
    if extra:
        for key, value in extra.items():
            if value is not None:
                payload[key] = value
    return payload


def _focus_payload(focus: PublicationManifestFocusV1 | None) -> dict | None:
    if focus is None:
        return None
    return {
        "source": focus.source,
        "annotationId": focus.annotation_id,
        "clipId": focus.clip_id,
        "videoId": focus.video_id,
        "seekMs": int(focus.seek_ms),
    }


def _preferred_published_transcript(rows: list[Transcript]) -> Transcript | None:
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            0 if row.source == TranscriptSource.MANUAL else 1,
            -((row.updated_at or row.created_at).timestamp()),
            str(row.id),
        ),
    )[0]


def _public_model_locator(object_id: UUID, model: ObjectModel) -> str | None:
    model_path = _existing_file(_normalized_path(model.storage_path))
    if model_path is None:
        return None
    cache_key = f"{model.revision_number}-{model.updated_at.isoformat()}"
    return f"/api/v1/public/objects/{object_id}/model/file?v={cache_key}"


def _video_stream_locator(video_id: UUID) -> str:
    return f"/api/v1/public/videos/{video_id}/stream"


def _public_summary(object_row: Object) -> str | None:
    metadata = object_row.metadata_json if isinstance(object_row.metadata_json, dict) else {}
    raw_value = metadata.get("public_standfirst")
    if isinstance(raw_value, str):
        trimmed = raw_value.strip()
        if trimmed:
            return trimmed

    if isinstance(object_row.description, str):
        trimmed = object_row.description.strip()
        if trimmed:
            return trimmed

    return None


def _normalized_actor_json(actor_json: dict | None) -> dict:
    if not isinstance(actor_json, dict):
        return {"type": "system", "trigger": "services.public_sync"}

    normalized = {str(key): value for key, value in actor_json.items() if value is not None}
    normalized.setdefault("type", "system")
    normalized.setdefault("trigger", "services.public_sync")
    return normalized


def _parse_timestamp(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _stable_package_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_media_path(path_value: str | Path) -> str:
    path = Path(path_value).resolve()
    media_root = Path(settings.media_root).resolve()
    try:
        return path.relative_to(media_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path is outside media_root: {path}") from exc


def _normalized_path(path_value: str | None) -> Path | None:
    if not isinstance(path_value, str):
        return None
    trimmed = path_value.strip()
    if not trimmed:
        return None
    return Path(trimmed)


def _existing_file(path: Path | None) -> Path | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return path


def _optional_nonempty_string(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None
