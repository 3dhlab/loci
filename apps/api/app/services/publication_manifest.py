from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from app.models.entities import Clip, ClipStatus, Object, ObjectModel, ObjectModelAnnotation, Video, VideoStatus
from app.services.media_clips import clip_poster_path_for_video
from app.services.media_transcode import transcode_output_path


@dataclass(slots=True)
class PublicationManifestBuildInput:
    object_row: Object
    model: ObjectModel | None
    videos: list[Video]
    annotations: list[ObjectModelAnnotation]
    clips: list[Clip]


@dataclass(slots=True)
class PublicationManifestObjectV1:
    internal_id: UUID
    public_id: str
    evidence_url: str


@dataclass(slots=True)
class PublicationManifestAnnotationV1:
    internal_id: UUID
    public_id: str
    evidence_url: str
    video_public_id: str


@dataclass(slots=True)
class PublicationManifestClipV1:
    internal_id: UUID
    public_id: str
    evidence_url: str
    video_public_id: str


@dataclass(slots=True)
class PublicationManifestVideoV1:
    internal_id: UUID
    public_id: str
    evidence_url: str


@dataclass(slots=True)
class PublicationManifestFocusV1:
    source: Literal["annotation", "clip", "video"]
    annotation_id: str | None
    clip_id: str | None
    video_id: str
    seek_ms: int


@dataclass(slots=True)
class PublicationManifestV1:
    schema_version: int
    package_type: Literal["object_package"]
    object_entry: PublicationManifestObjectV1
    default_focus: PublicationManifestFocusV1 | None
    annotations_by_internal_id: dict[UUID, PublicationManifestAnnotationV1]
    annotations_by_public_id: dict[str, PublicationManifestAnnotationV1]
    clips_by_public_id: dict[str, PublicationManifestClipV1]
    videos_by_public_id: dict[str, PublicationManifestVideoV1]


def build_publication_manifest(input_data: PublicationManifestBuildInput) -> PublicationManifestV1 | None:
    object_public_id = _optional_nonempty_string(input_data.object_row.website_object_id)
    if object_public_id is None:
        return None
    if not input_data.object_row.is_published or not input_data.object_row.is_embed_ready:
        return None
    if input_data.model is None or _public_model_is_available(input_data.object_row.id, input_data.model) is None:
        return None

    package_videos = [
        video
        for video in input_data.videos
        if video.object_id == input_data.object_row.id
        and video.is_published
        and video.status == VideoStatus.READY
        and _optional_nonempty_string(video.stable_video_id) is not None
        and _video_stream_url(video) is not None
    ]
    if not package_videos:
        return None

    videos_by_uuid = {video.id: video for video in package_videos}
    video_entries = {
        video.stable_video_id: PublicationManifestVideoV1(
            internal_id=video.id,
            public_id=video.stable_video_id,
            evidence_url=_build_evidence_url(object_public_id, video_id=video.stable_video_id),
        )
        for video in package_videos
    }

    package_clips = [
        clip
        for clip in input_data.clips
        if clip.video_id in videos_by_uuid
        and clip.status == ClipStatus.COMPLETE
        and _optional_nonempty_string(clip.website_clip_id) is not None
        and _clip_stream_url(clip) is not None
        and _clip_poster_url(clip) is not None
    ]
    clips_by_uuid = {clip.id: clip for clip in package_clips}
    clip_entries = {
        clip.website_clip_id.strip(): PublicationManifestClipV1(
            internal_id=clip.id,
            public_id=clip.website_clip_id.strip(),
            evidence_url=_build_evidence_url(object_public_id, clip_id=clip.website_clip_id.strip()),
            video_public_id=videos_by_uuid[clip.video_id].stable_video_id,
        )
        for clip in package_clips
        if isinstance(clip.website_clip_id, str) and clip.website_clip_id.strip()
    }

    annotation_rows = [
        annotation
        for annotation in input_data.annotations
        if annotation.object_id == input_data.object_row.id
        and annotation.is_published
        and annotation.video_id in videos_by_uuid
        and _optional_nonempty_string(annotation.website_annotation_id) is not None
    ]
    annotation_entries_by_internal_id: dict[UUID, PublicationManifestAnnotationV1] = {}
    annotation_entries_by_public_id: dict[str, PublicationManifestAnnotationV1] = {}
    for annotation in annotation_rows:
        public_annotation_id = annotation.website_annotation_id.strip()
        video = videos_by_uuid[annotation.video_id]
        entry = PublicationManifestAnnotationV1(
            internal_id=annotation.id,
            public_id=public_annotation_id,
            evidence_url=_build_evidence_url(object_public_id, annotation_id=public_annotation_id),
            video_public_id=video.stable_video_id,
        )
        annotation_entries_by_internal_id[annotation.id] = entry
        annotation_entries_by_public_id[public_annotation_id] = entry

    default_focus = _default_focus(
        annotations=annotation_rows,
        annotation_entries_by_internal_id=annotation_entries_by_internal_id,
        clip_entries=clip_entries,
        clips_by_uuid=clips_by_uuid,
        videos_by_uuid=videos_by_uuid,
    )

    return PublicationManifestV1(
        schema_version=1,
        package_type="object_package",
        object_entry=PublicationManifestObjectV1(
            internal_id=input_data.object_row.id,
            public_id=object_public_id,
            evidence_url=_build_evidence_url(object_public_id),
        ),
        default_focus=default_focus,
        annotations_by_internal_id=annotation_entries_by_internal_id,
        annotations_by_public_id=annotation_entries_by_public_id,
        clips_by_public_id=clip_entries,
        videos_by_public_id=video_entries,
    )


def _default_focus(
    *,
    annotations: list[ObjectModelAnnotation],
    annotation_entries_by_internal_id: dict[UUID, PublicationManifestAnnotationV1],
    clip_entries: dict[str, PublicationManifestClipV1],
    clips_by_uuid: dict[UUID, Clip],
    videos_by_uuid: dict[UUID, Video],
) -> PublicationManifestFocusV1 | None:
    if annotations:
        annotation = annotations[0]
        annotation_entry = annotation_entries_by_internal_id.get(annotation.id)
        video = videos_by_uuid.get(annotation.video_id)
        if annotation_entry is not None and video is not None:
            related_clips = _annotation_related_clips(annotation, clips_by_uuid)
            selected_clip = related_clips[0] if related_clips else None
            selected_clip_id = None
            if selected_clip is not None and isinstance(selected_clip.website_clip_id, str):
                normalized_clip_id = selected_clip.website_clip_id.strip()
                if normalized_clip_id in clip_entries:
                    selected_clip_id = normalized_clip_id
            return PublicationManifestFocusV1(
                source="annotation",
                annotation_id=annotation_entry.public_id,
                clip_id=selected_clip_id,
                video_id=video.stable_video_id,
                seek_ms=max(0, int(annotation.start_ms)),
            )

    if clip_entries:
        first_clip = next(iter(clip_entries.values()))
        return PublicationManifestFocusV1(
            source="clip",
            annotation_id=None,
            clip_id=first_clip.public_id,
            video_id=first_clip.video_public_id,
            seek_ms=_clip_seek_ms(first_clip.public_id, clips_by_uuid),
        )

    if videos_by_uuid:
        first_video = next(iter(videos_by_uuid.values()))
        return PublicationManifestFocusV1(
            source="video",
            annotation_id=None,
            clip_id=None,
            video_id=first_video.stable_video_id,
            seek_ms=0,
        )

    return None


def _annotation_related_clips(annotation: ObjectModelAnnotation, clips_by_uuid: dict[UUID, Clip]) -> list[Clip]:
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

    related_clips: list[Clip] = []
    seen: set[UUID] = set()
    for candidate_id in candidate_ids:
        if candidate_id in seen:
            continue
        clip = clips_by_uuid.get(candidate_id)
        if clip is None:
            continue
        seen.add(candidate_id)
        related_clips.append(clip)
    return related_clips


def _build_evidence_url(
    object_public_id: str,
    *,
    annotation_id: str | None = None,
    clip_id: str | None = None,
    video_id: str | None = None,
    seek_ms: int | None = None,
) -> str:
    query: list[tuple[str, str]] = []
    if clip_id is not None:
        query.append(("clip", clip_id))
    elif annotation_id is not None:
        query.append(("annotation", annotation_id))
    elif video_id is not None:
        query.append(("video", video_id))
        if seek_ms is not None and seek_ms > 0:
            query.append(("t", str(int(seek_ms))))

    encoded = urlencode(query)
    base_path = f"/evidence/objects/{object_public_id}"
    return f"{base_path}?{encoded}" if encoded else base_path


def _optional_nonempty_string(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _public_model_is_available(object_id: UUID, model: ObjectModel) -> str | None:
    storage_path = Path(model.storage_path)
    if not storage_path.exists() or not storage_path.is_file():
        return None
    cache_key = f"{model.revision_number}-{model.updated_at.isoformat()}"
    return f"/api/v1/public/objects/{object_id}/model/file?v={cache_key}"


def _video_stream_url(video: Video) -> str | None:
    playback_path = transcode_output_path(str(video.id))
    if not playback_path.exists() or not playback_path.is_file():
        return None
    return f"/api/v1/public/videos/{video.id}/stream"


def _clip_stream_url(clip: Clip) -> str | None:
    clip_path = _existing_file(_normalized_path(clip.output_mp4_path))
    if clip_path is None or clip.website_clip_id is None:
        return None
    return f"/api/public/clips/{clip.website_clip_id}/stream"


def _clip_poster_url(clip: Clip) -> str | None:
    if clip.website_clip_id is None:
        return None
    poster_path = clip_poster_path_for_video(clip.output_mp4_path or "")
    if not poster_path.exists() or not poster_path.is_file():
        return None
    return f"/api/public/clips/{clip.website_clip_id}/poster"


def _clip_seek_ms(clip_public_id: str, clips_by_uuid: dict[UUID, Clip]) -> int:
    for clip in clips_by_uuid.values():
        if clip.website_clip_id == clip_public_id:
            return max(0, int(clip.start_ms))
    return 0


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