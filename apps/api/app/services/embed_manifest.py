from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Clip, ClipStatus, Object, ObjectModel, ObjectModelAnnotation, Project, Video, VideoStatus
from app.schemas.embed import EmbedCameraView, PublicAnnotationV1, PublicClipV1, PublicObjectManifestV1
from app.services.media_clips import clip_poster_path_for_video
from app.services.public_media_cache import model_cache_token
from app.services.public_media_paths import resolve_public_media_file
from app.services.object_model_variants import lookup_public_delivery_capability
from app.services.public_object_ids import resolve_website_object_id_alias


class PublicObjectManifestNotFoundError(RuntimeError):
    pass


@dataclass(slots=True)
class EmbedObjectContext:
    project: Project
    object_row: Object
    model: ObjectModel
    summary: str
    initial_view: EmbedCameraView
    annotations: list[ObjectModelAnnotation]
    clips: list[Clip]


def embed_public_base_url() -> str:
    configured = (settings.semantic_embed_public_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return settings.api_base_url.rstrip("/")


def manifest_cache_control() -> str:
    environment = settings.semantic_env.strip().lower()
    if environment == "production":
        return "public, max-age=300, stale-while-revalidate=60"
    if environment == "staging":
        return "public, max-age=30, stale-while-revalidate=30"
    return "no-store"


def asset_cache_control() -> str:
    environment = settings.semantic_env.strip().lower()
    if environment == "production":
        return "public, max-age=3600"
    if environment == "staging":
        return "public, max-age=60"
    return "no-store"


def manifest_etag(manifest: PublicObjectManifestV1) -> str | None:
    environment = settings.semantic_env.strip().lower()
    if environment not in {"staging", "production"}:
        return None
    payload = manifest.model_dump_json(exclude_none=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f'W/"{digest}"'


def get_embed_object_context(db: Session, website_object_id: str) -> EmbedObjectContext:
    normalized_object_id = website_object_id.strip()
    if not normalized_object_id:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    resolved_object_id = resolve_website_object_id_alias(normalized_object_id)

    object_row = db.execute(
        select(Object).where(Object.website_object_id == resolved_object_id)
    ).scalar_one_or_none()
    if object_row is None or not object_row.is_published or not object_row.is_embed_ready:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    project = db.get(Project, object_row.project_id)
    if project is None or not isinstance(project.website_slug, str) or not project.website_slug.strip():
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    summary = _normalized_public_summary(object_row)
    if summary is None:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    model = db.execute(
        select(ObjectModel).where(ObjectModel.object_id == object_row.id, ObjectModel.is_published.is_(True))
    ).scalar_one_or_none()
    if model is None:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    if resolve_public_media_file(model.public_poster_path) is None:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    initial_view = _normalized_camera_view(model.default_camera_json)
    if initial_view is None:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    annotation_rows = db.execute(
        select(ObjectModelAnnotation)
        .where(
            ObjectModelAnnotation.object_id == object_row.id,
            ObjectModelAnnotation.is_published.is_(True),
        )
        .order_by(ObjectModelAnnotation.created_at.asc(), ObjectModelAnnotation.id.asc())
    ).scalars().all()

    emitted_annotations: list[ObjectModelAnnotation] = []
    for annotation in annotation_rows:
        if _optional_nonempty_string(annotation.website_annotation_id) is None:
            continue
        _ = _normalized_vec3(
            annotation.point_x,
            annotation.point_y,
            annotation.point_z,
        )
        _ = _normalized_relations(annotation.website_relations_json)
        emitted_annotations.append(annotation)

    if not emitted_annotations:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")

    clip_rows = db.execute(
        select(Clip)
        .join(Video, Video.id == Clip.video_id)
        .where(
            Video.object_id == object_row.id,
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Clip.status == ClipStatus.COMPLETE,
            Clip.website_clip_id.is_not(None),
        )
        .order_by(Clip.created_at.asc(), Clip.id.asc())
    ).scalars().all()

    emitted_clips: list[Clip] = []
    for clip in clip_rows:
        if _optional_nonempty_string(clip.website_clip_id) is None:
            continue
        if _clip_stream_path(clip) is None:
            continue
        if _clip_poster_path(clip) is None:
            continue
        emitted_clips.append(clip)

    return EmbedObjectContext(
        project=project,
        object_row=object_row,
        model=model,
        summary=summary,
        initial_view=initial_view,
        annotations=emitted_annotations,
        clips=emitted_clips,
    )


def build_public_object_manifest(db: Session, website_object_id: str) -> PublicObjectManifestV1:
    context = get_embed_object_context(db, website_object_id)
    base_url = embed_public_base_url()
    model_src = _public_model_url(db, base_url, context.object_row.id, context.model)
    delivery_capability = lookup_public_delivery_capability(
        db,
        context.model.id,
        variants_enabled=getattr(settings, "public_model_variants_enabled", False),
    )
    clips_by_uuid = {clip.id: clip for clip in context.clips}

    annotations: list[PublicAnnotationV1] = []
    for order, annotation in enumerate(context.annotations, start=1):
        relations = _normalized_relations(annotation.website_relations_json)
        annotation_title = _normalized_nonempty_string(annotation.title)
        annotation_body = _optional_nonempty_string(annotation.description)
        annotation_camera = _normalized_camera_view(annotation.camera_json)
        related_clip_ids = _annotation_related_clip_ids(annotation, clips_by_uuid)
        related_clip_id = related_clip_ids[0] if related_clip_ids else None
        normal = None
        if annotation.normal_x is not None and annotation.normal_y is not None and annotation.normal_z is not None:
            normal = _normalized_vec3(annotation.normal_x, annotation.normal_y, annotation.normal_z)

        annotations.append(
            PublicAnnotationV1(
                id=_normalized_nonempty_string(annotation.website_annotation_id),
                label=annotation_title,
                title=annotation_title,
                body=annotation_body,
                position=_normalized_vec3(annotation.point_x, annotation.point_y, annotation.point_z),
                normal=normal,
                camera=annotation_camera,
                order=order,
                relatedClipId=related_clip_id,
                relatedClipIds=related_clip_ids,
                relatedPublicationIds=relations["publication_ids"],
                relatedProjectIds=relations["project_ids"],
                relatedLocationIds=relations["location_ids"],
            )
        )

    clips = [
        PublicClipV1(
            id=_normalized_nonempty_string(clip.website_clip_id),
            title=_clip_title(context.object_row.name, clip),
            src=f"{base_url}/api/public/clips/{clip.website_clip_id}/stream",
            poster=f"{base_url}/api/public/clips/{clip.website_clip_id}/poster",
            startTime=_ms_to_seconds(clip.start_ms),
            endTime=_ms_to_seconds(clip.end_ms),
            transcript=_normalized_clip_transcript(clip),
            description=_optional_nonempty_string(clip.citation_text),
        )
        for clip in context.clips
    ]

    return PublicObjectManifestV1(
        manifestVersion=1,
        id=context.object_row.website_object_id.strip(),
        title=context.object_row.name.strip(),
        summary=context.summary,
        posterSrc=f"{base_url}/api/public/objects/{context.object_row.website_object_id}/poster",
        embedUrl=f"{base_url}/embed/object/{context.object_row.website_object_id}",
        provider="Semantic",
        initialView=context.initial_view,
        modelSrc=model_src,
        modelDelivery=(
            delivery_capability.public_payload(camel_case=True)
            if delivery_capability is not None
            else None
        ),
        annotations=annotations,
        clips=clips,
    )


def _normalized_public_summary(object_row: Object) -> str | None:
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


def _normalized_nonempty_string(value: str | None) -> str:
    if not isinstance(value, str):
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")
    trimmed = value.strip()
    if not trimmed:
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")
    return trimmed


def _optional_nonempty_string(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalized_nonempty_string(value)


def _normalized_path(path_value: str | None) -> Path | None:
    if not isinstance(path_value, str):
        return None
    trimmed = path_value.strip()
    if not trimmed:
        return None
    return Path(trimmed)


def _normalized_camera_view(camera_payload: dict | None) -> EmbedCameraView | None:
    if not isinstance(camera_payload, dict):
        return None
    position = camera_payload.get("position")
    target = camera_payload.get("target")
    if not isinstance(position, (list, tuple)) or not isinstance(target, (list, tuple)):
        return None
    try:
        return EmbedCameraView(
            position=_normalized_vec3(position[0], position[1], position[2]),
            target=_normalized_vec3(target[0], target[1], target[2]),
        )
    except (IndexError, PublicObjectManifestNotFoundError):
        return None


def _normalized_vec3(x, y, z) -> tuple[float, float, float]:
    values = (float(x), float(y), float(z))
    if not all(math.isfinite(value) for value in values):
        raise PublicObjectManifestNotFoundError("Embed-ready object not found")
    return values


def _normalized_relations(payload: dict | None) -> dict[str, list[str]]:
    data = payload if isinstance(payload, dict) else {}
    return {
        "publication_ids": _normalized_relation_list(data.get("publication_ids")),
        "project_ids": _normalized_relation_list(data.get("project_ids")),
        "location_ids": _normalized_relation_list(data.get("location_ids")),
    }


def _normalized_relation_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        trimmed = item.strip()
        if trimmed:
            normalized.append(trimmed)
    return normalized


def _public_model_url(db: Session, base_url: str, object_id: UUID, model: ObjectModel) -> str | None:
    storage_path = Path(model.storage_path)
    if not storage_path.exists() or not storage_path.is_file():
        return None
    return f"{base_url}/api/v1/public/objects/{object_id}/model/file?v={model_cache_token(db, model)}"


def _clip_title(object_name: str, clip: Clip) -> str:
    return f"{object_name} clip {_format_clip_seconds(clip.start_ms)}-{_format_clip_seconds(clip.end_ms)}"


def _format_clip_seconds(value_ms: int) -> str:
    total_seconds = max(0, int(value_ms // 1000))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _ms_to_seconds(value_ms: int | None) -> float | None:
    if value_ms is None:
        return None
    return round(max(0, int(value_ms)) / 1000.0, 3)


def _normalized_clip_transcript(clip: Clip) -> str | None:
    if isinstance(clip.transcript_excerpt, str):
        trimmed = clip.transcript_excerpt.strip()
        if trimmed:
            return trimmed
    transcript_path = _normalized_path(clip.metadata_json_path)
    if transcript_path is None or not transcript_path.exists() or not transcript_path.is_file():
        return None
    trimmed = transcript_path.read_text(encoding="utf-8").strip()
    return trimmed or None


def _clip_stream_path(clip: Clip) -> Path | None:
    return _existing_file(_normalized_path(clip.output_mp4_path))


def _clip_poster_path(clip: Clip) -> Path | None:
    if not clip.output_mp4_path:
        return None
    return _existing_file(clip_poster_path_for_video(clip.output_mp4_path))


def _existing_file(path: Path | None) -> Path | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return path


def _annotation_related_clip_ids(annotation: ObjectModelAnnotation, clips_by_uuid: dict[UUID, Clip]) -> list[str]:
    candidate_ids: list[UUID] = []
    if annotation.clip_id is not None:
        candidate_ids.append(annotation.clip_id)

    playlist = annotation.playlist_json if isinstance(annotation.playlist_json, list) else []
    for entry in playlist:
        if not isinstance(entry, dict) or not entry.get("clip_id"):
            continue
        try:
            candidate_ids.append(UUID(str(entry["clip_id"])))
        except ValueError:
            continue

    related_clip_ids: list[str] = []
    seen_clip_ids: set[str] = set()
    for candidate_id in candidate_ids:
        clip = clips_by_uuid.get(candidate_id)
        if clip is None:
            continue
        website_clip_id = _optional_nonempty_string(clip.website_clip_id)
        if website_clip_id is None or website_clip_id in seen_clip_ids:
            continue
        seen_clip_ids.add(website_clip_id)
        related_clip_ids.append(website_clip_id)
    return related_clip_ids


def manifest_payload_bytes(manifest: PublicObjectManifestV1) -> bytes:
    return json.dumps(manifest.model_dump(exclude_none=True), separators=(",", ":")).encode("utf-8")
