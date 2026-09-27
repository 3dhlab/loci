from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import (
    Clip,
    ClipStatus,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    Project,
    Segment,
    TitleCardObservation,
    Transcript,
    TranscriptSource,
    Video,
    VideoStatus,
)
from app.schemas.object_model import CameraViewPayload
from app.schemas.public import (
    PublicEvidenceAnnotationResponse,
    PublicEvidenceCitationAttributionResponse,
    PublicEvidenceCitationAttributionTimelineEntryResponse,
    PublicEvidenceClipDetailResponse,
    PublicEvidenceClipSummaryResponse,
    PublicEvidenceFocusResponse,
    PublicEvidenceModelResponse,
    PublicEvidenceObjectSummaryResponse,
    PublicEvidencePageFrameResponse,
    PublicEvidencePageResponse,
    PublicEvidencePlaybackResponse,
    PublicEvidenceTranscriptResponse,
    PublicEvidenceTranscriptSegmentResponse,
)
from app.services.media_clips import clip_poster_path_for_video
from app.services.media_transcode import video_playback_path
from app.services.publication_manifest import PublicationManifestBuildInput, PublicationManifestV1, build_publication_manifest
from app.services.public_media_cache import model_cache_token, video_cache_token
from app.services.public_media_paths import resolve_public_media_file
from app.services.object_model_variants import lookup_public_delivery_capability
from app.services.public_object_ids import resolve_website_object_id_alias


class PublicEvidenceNotFoundError(RuntimeError):
    pass


class PublicEvidenceConflictError(RuntimeError):
    pass


_RELIABLE_TITLE_CARD_CONFIDENCES = {"high", "medium"}
# Public attribution trusts live OCR first, then reviewed pilot artifacts, and
# only falls back to deterministic fixtures when no stronger eligible source wins.
_TITLE_CARD_PUBLIC_SOURCE_PRIORITY = {
    "vision_ocr": 3,
    "pilot_artifact": 2,
    "fixture_import": 1,
}
_TITLE_CARD_PUBLIC_SOURCE_ALIASES = {
    "title_card_ocr": "vision_ocr",
}


@dataclass(slots=True)
class ResolvedEvidenceFocus:
    source: Literal["clip", "annotation", "video", "t", "default"]
    selected_video: Video
    selected_annotation: ObjectModelAnnotation | None
    selected_clip: Clip | None
    seek_ms: int
    start_ms: int
    end_ms: int | None


def build_public_evidence_page(
    db: Session,
    website_object_id: str,
    *,
    clip_id: str | None = None,
    annotation_id: str | None = None,
    video_id: str | None = None,
    seek_ms: int | None = None,
) -> PublicEvidencePageResponse:
    normalized_object_id = _normalized_nonempty_string(website_object_id, "Published object not found")
    _validate_focus_params(clip_id=clip_id, annotation_id=annotation_id, video_id=video_id, seek_ms=seek_ms)

    object_row, project = _public_object_context(db, normalized_object_id)
    canonical_object_id = _normalized_nonempty_string(object_row.website_object_id, "Published object not found")
    model = _public_model(object_row.id, db)
    if model is None:
        raise PublicEvidenceNotFoundError("Published object model not found")

    model_url = _public_model_url(db, object_row.id, model)
    if model_url is None:
        raise PublicEvidenceNotFoundError("Published object model file is missing")
    videos = _public_videos(db, object_row.id)
    videos_by_uuid = {video.id: video for video in videos}
    videos_by_stable_id = {video.stable_video_id: video for video in videos}

    clips = _public_clips(db, object_row.id)
    clips_by_uuid = {clip.id: clip for clip in clips}
    clips_by_website_id = {
        clip.website_clip_id.strip(): clip
        for clip in clips
        if isinstance(clip.website_clip_id, str) and clip.website_clip_id.strip()
    }

    annotations = _public_annotations(db, object_row.id)
    annotations = [annotation for annotation in annotations if annotation.video_id in videos_by_uuid]
    annotations_by_website_id = {
        annotation.website_annotation_id.strip(): annotation
        for annotation in annotations
        if isinstance(annotation.website_annotation_id, str) and annotation.website_annotation_id.strip()
    }

    publication_manifest = build_publication_manifest(
        PublicationManifestBuildInput(
            object_row=object_row,
            model=model,
            videos=videos,
            annotations=annotations,
            clips=clips,
        )
    )
    if publication_manifest is None:
        raise PublicEvidenceNotFoundError("Published evidence is unavailable for this object")
    _validate_manifest_focus_membership(
        publication_manifest,
        clip_id=clip_id,
        annotation_id=annotation_id,
        video_id=video_id,
    )

    resolved = _resolve_focus(
        clip_id=clip_id,
        annotation_id=annotation_id,
        video_id=video_id,
        seek_ms=seek_ms,
        annotations=annotations,
        annotations_by_website_id=annotations_by_website_id,
        clips=clips,
        clips_by_uuid=clips_by_uuid,
        clips_by_website_id=clips_by_website_id,
        videos=videos,
        videos_by_uuid=videos_by_uuid,
        videos_by_stable_id=videos_by_stable_id,
    )

    transcript = _preferred_public_transcript(db, resolved.selected_video.id)
    segments = _transcript_segments(db, transcript.id) if transcript is not None else []
    object_summary = _public_summary(object_row)
    selected_camera = _camera_payload(resolved.selected_annotation.camera_json) if resolved.selected_annotation is not None else None
    default_camera = _camera_payload(model.default_camera_json)
    delivery_capability = lookup_public_delivery_capability(
        db,
        model.id,
        variants_enabled=getattr(settings, "public_model_variants_enabled", False),
    )

    selected_annotation_response = (
        _annotation_response(resolved.selected_annotation, clips_by_uuid) if resolved.selected_annotation is not None else None
    )
    selected_clip_response = _clip_detail_response(object_row.name, resolved.selected_clip) if resolved.selected_clip is not None else None
    citation_attribution = _citation_attribution_response(db, resolved)
    citation_attribution_timeline = _citation_attribution_timeline_response(db, resolved.selected_video)

    return PublicEvidencePageResponse(
        canonical_url=_canonical_url(canonical_object_id, resolved),
        page=PublicEvidencePageFrameResponse(
            title=_page_title(object_row.name, resolved),
            summary=_page_summary(object_summary, resolved),
        ),
        object=PublicEvidenceObjectSummaryResponse(
            id=canonical_object_id,
            title=object_row.name,
            summary=object_summary,
            poster_url=_public_object_poster_url(object_row, model),
            external_url=_optional_nonempty_string(object_row.external_url),
            project_slug=_optional_nonempty_string(project.website_slug),
            project_name=project.name,
        ),
        focus=PublicEvidenceFocusResponse(
            source=resolved.source,
            object_id=canonical_object_id,
            annotation_id=selected_annotation_response.id if selected_annotation_response is not None else None,
            clip_id=selected_clip_response.id if selected_clip_response is not None else None,
            video_id=resolved.selected_video.stable_video_id,
            seek_ms=resolved.seek_ms,
            start_ms=resolved.start_ms,
            end_ms=resolved.end_ms,
        ),
        model=PublicEvidenceModelResponse(
            model_url=model_url,
            delivery_capabilities=(
                delivery_capability.public_payload() if delivery_capability is not None else None
            ),
            default_camera=default_camera,
            selected_camera=selected_camera or default_camera,
        ),
        playback=PublicEvidencePlaybackResponse(
            video_id=resolved.selected_video.stable_video_id,
            video_title=resolved.selected_video.title,
            # Versioned via the shared helper so replacing a normalized playback
            # asset invalidates cached bodies. `_public_videos` only yields videos
            # whose playback file exists, so the fallback is unreachable in practice
            # and exists to keep page assembly total.
            video_stream_url=(
                _video_stream_url(resolved.selected_video)
                or f"/api/v1/public/videos/{resolved.selected_video.id}/stream"
            ),
            seek_ms=resolved.seek_ms,
            window_start_ms=resolved.start_ms,
            window_end_ms=resolved.end_ms,
            duration_ms=_positive_duration_ms(resolved.selected_video.duration_ms),
        ),
        selected_annotation=selected_annotation_response,
        selected_clip=selected_clip_response,
        annotations=[_annotation_response(annotation, clips_by_uuid) for annotation in annotations],
        clips=[_clip_summary_response(object_row.name, clip) for clip in clips],
        transcript=(
            PublicEvidenceTranscriptResponse(
                video_id=resolved.selected_video.stable_video_id,
                segments=[
                    PublicEvidenceTranscriptSegmentResponse(
                        position=segment.position,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=segment.text,
                    )
                    for segment in segments
                ],
            )
            if transcript is not None
            else None
        ),
        citation_attribution=citation_attribution,
        citation_attribution_timeline=citation_attribution_timeline,
    )


def _validate_focus_params(
    *,
    clip_id: str | None,
    annotation_id: str | None,
    video_id: str | None,
    seek_ms: int | None,
) -> None:
    has_clip = bool(_optional_nonempty_string(clip_id))
    has_annotation = bool(_optional_nonempty_string(annotation_id))
    has_video = bool(_optional_nonempty_string(video_id))
    has_seek = seek_ms is not None

    if has_clip and (has_annotation or has_video or has_seek):
        raise PublicEvidenceConflictError(
            "Conflicting focus parameters. clip_id cannot be combined with annotation_id, video_id, or seek_ms."
        )
    if has_annotation and (has_video or has_seek):
        raise PublicEvidenceConflictError(
            "Conflicting focus parameters. annotation_id cannot be combined with video_id or seek_ms."
        )


def _validate_manifest_focus_membership(
    publication_manifest: PublicationManifestV1,
    *,
    clip_id: str | None,
    annotation_id: str | None,
    video_id: str | None,
) -> None:
    normalized_clip_id = _optional_nonempty_string(clip_id)
    normalized_annotation_id = _optional_nonempty_string(annotation_id)
    normalized_video_id = _optional_nonempty_string(video_id)

    if normalized_clip_id is not None and normalized_clip_id not in publication_manifest.clips_by_public_id:
        raise PublicEvidenceNotFoundError("Published clip not found for this object")
    if normalized_annotation_id is not None and normalized_annotation_id not in publication_manifest.annotations_by_public_id:
        raise PublicEvidenceNotFoundError("Published annotation not found for this object")
    if normalized_video_id is not None and normalized_video_id not in publication_manifest.videos_by_public_id:
        raise PublicEvidenceNotFoundError("Published video not found for this object")


def _resolve_focus(
    *,
    clip_id: str | None,
    annotation_id: str | None,
    video_id: str | None,
    seek_ms: int | None,
    annotations: list[ObjectModelAnnotation],
    annotations_by_website_id: dict[str, ObjectModelAnnotation],
    clips: list[Clip],
    clips_by_uuid: dict[UUID, Clip],
    clips_by_website_id: dict[str, Clip],
    videos: list[Video],
    videos_by_uuid: dict[UUID, Video],
    videos_by_stable_id: dict[str, Video],
) -> ResolvedEvidenceFocus:
    normalized_clip_id = _optional_nonempty_string(clip_id)
    normalized_annotation_id = _optional_nonempty_string(annotation_id)
    normalized_video_id = _optional_nonempty_string(video_id)

    if normalized_clip_id is not None:
        clip = clips_by_website_id.get(normalized_clip_id)
        if clip is None:
            raise PublicEvidenceNotFoundError("Published clip not found for this object")
        selected_video = videos_by_uuid.get(clip.video_id)
        if selected_video is None:
            raise PublicEvidenceNotFoundError("Published video not found for this clip")
        related_annotation = _unique_annotation_for_clip(clip, annotations, clips_by_uuid)
        return ResolvedEvidenceFocus(
            source="clip",
            selected_video=selected_video,
            selected_annotation=related_annotation,
            selected_clip=clip,
            seek_ms=clip.start_ms,
            start_ms=clip.start_ms,
            end_ms=clip.end_ms,
        )

    if normalized_annotation_id is not None:
        annotation = annotations_by_website_id.get(normalized_annotation_id)
        if annotation is None:
            raise PublicEvidenceNotFoundError("Published annotation not found for this object")
        selected_video = videos_by_uuid.get(annotation.video_id)
        if selected_video is None:
            raise PublicEvidenceNotFoundError("Published video not found for this annotation")
        related_clip = _annotation_related_clips(annotation, clips_by_uuid)[:1]
        return ResolvedEvidenceFocus(
            source="annotation",
            selected_video=selected_video,
            selected_annotation=annotation,
            selected_clip=related_clip[0] if related_clip else None,
            seek_ms=annotation.start_ms,
            start_ms=annotation.start_ms,
            end_ms=annotation.end_ms,
        )

    if normalized_video_id is not None:
        video = videos_by_stable_id.get(normalized_video_id)
        if video is None:
            raise PublicEvidenceNotFoundError("Published video not found for this object")
        resolved_seek_ms = max(0, int(seek_ms or 0))
        return ResolvedEvidenceFocus(
            source="video",
            selected_video=video,
            selected_annotation=None,
            selected_clip=None,
            seek_ms=resolved_seek_ms,
            start_ms=resolved_seek_ms,
            end_ms=None,
        )

    if seek_ms is not None:
        default_video = _default_video(annotations=annotations, clips=clips, videos=videos, videos_by_uuid=videos_by_uuid)
        if default_video is None:
            raise PublicEvidenceNotFoundError("Published evidence is unavailable for this object")
        resolved_seek_ms = max(0, int(seek_ms))
        return ResolvedEvidenceFocus(
            source="t",
            selected_video=default_video,
            selected_annotation=None,
            selected_clip=None,
            seek_ms=resolved_seek_ms,
            start_ms=resolved_seek_ms,
            end_ms=None,
        )

    if annotations:
        annotation = annotations[0]
        selected_video = videos_by_uuid.get(annotation.video_id)
        if selected_video is None:
            raise PublicEvidenceNotFoundError("Published evidence is unavailable for this object")
        related_clip = _annotation_related_clips(annotation, clips_by_uuid)[:1]
        return ResolvedEvidenceFocus(
            source="default",
            selected_video=selected_video,
            selected_annotation=annotation,
            selected_clip=related_clip[0] if related_clip else None,
            seek_ms=annotation.start_ms,
            start_ms=annotation.start_ms,
            end_ms=annotation.end_ms,
        )

    if clips:
        clip = clips[0]
        selected_video = videos_by_uuid.get(clip.video_id)
        if selected_video is None:
            raise PublicEvidenceNotFoundError("Published evidence is unavailable for this object")
        return ResolvedEvidenceFocus(
            source="default",
            selected_video=selected_video,
            selected_annotation=None,
            selected_clip=clip,
            seek_ms=clip.start_ms,
            start_ms=clip.start_ms,
            end_ms=clip.end_ms,
        )

    default_video = _default_video(annotations=annotations, clips=clips, videos=videos, videos_by_uuid=videos_by_uuid)
    if default_video is None:
        raise PublicEvidenceNotFoundError("Published evidence is unavailable for this object")

    return ResolvedEvidenceFocus(
        source="default",
        selected_video=default_video,
        selected_annotation=None,
        selected_clip=None,
        seek_ms=0,
        start_ms=0,
        end_ms=None,
    )


def _default_video(
    *,
    annotations: list[ObjectModelAnnotation],
    clips: list[Clip],
    videos: list[Video],
    videos_by_uuid: dict[UUID, Video],
) -> Video | None:
    if annotations:
        selected = videos_by_uuid.get(annotations[0].video_id)
        if selected is not None:
            return selected
    if clips:
        selected = videos_by_uuid.get(clips[0].video_id)
        if selected is not None:
            return selected
    return videos[0] if videos else None


def _public_object_context(db: Session, website_object_id: str) -> tuple[Object, Project]:
    resolved_object_id = resolve_website_object_id_alias(website_object_id)
    row = db.execute(
        select(Object, Project)
        .join(Project, Project.id == Object.project_id)
        .where(
            Object.website_object_id == resolved_object_id,
            Object.is_published.is_(True),
            Object.is_embed_ready.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise PublicEvidenceNotFoundError("Published object not found")
    return row[0], row[1]


def _public_model(object_id: UUID, db: Session) -> ObjectModel | None:
    return db.execute(
        select(ObjectModel)
        .where(ObjectModel.object_id == object_id, ObjectModel.is_published.is_(True))
    ).scalar_one_or_none()


def _public_object_poster_url(object_row: Object, model: ObjectModel) -> str | None:
    website_object_id = (object_row.website_object_id or "").strip()
    if not website_object_id:
        return None
    if resolve_public_media_file(model.public_poster_path) is None:
        return None
    return f"/api/public/objects/{website_object_id}/poster"


def _public_annotations(db: Session, object_id: UUID) -> list[ObjectModelAnnotation]:
    return db.execute(
        select(ObjectModelAnnotation)
        .join(Video, Video.id == ObjectModelAnnotation.video_id)
        .where(
            ObjectModelAnnotation.object_id == object_id,
            ObjectModelAnnotation.is_published.is_(True),
            ObjectModelAnnotation.website_annotation_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
        )
        .order_by(ObjectModelAnnotation.created_at.asc(), ObjectModelAnnotation.id.asc())
    ).scalars().all()


def _public_clips(db: Session, object_id: UUID) -> list[Clip]:
    clip_rows = db.execute(
        select(Clip)
        .join(Video, Video.id == Clip.video_id)
        .join(Object, Object.id == Video.object_id)
        .where(
            Object.id == object_id,
            Object.is_published.is_(True),
            Object.is_embed_ready.is_(True),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Clip.status == ClipStatus.COMPLETE,
            Clip.website_clip_id.is_not(None),
        )
        .order_by(Clip.created_at.asc(), Clip.id.asc())
    ).scalars().all()

    return [clip for clip in clip_rows if _clip_stream_url(clip) is not None and _clip_poster_url(clip) is not None]


def _public_videos(db: Session, object_id: UUID) -> list[Video]:
    video_rows = db.execute(
        select(Video)
        .join(Object, Object.id == Video.object_id)
        .where(
            Object.id == object_id,
            Object.is_published.is_(True),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
        )
        .order_by(Video.created_at.asc(), Video.id.asc())
    ).scalars().all()
    return [video for video in video_rows if _video_stream_url(video) is not None]


def _preferred_public_transcript(db: Session, video_id: UUID) -> Transcript | None:
    rows = db.execute(
        select(Transcript)
        .where(Transcript.video_id == video_id, Transcript.is_published.is_(True))
        .order_by(Transcript.updated_at.desc())
    ).scalars().all()
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            0 if row.source == TranscriptSource.MANUAL else 1,
            -(row.updated_at or row.created_at).timestamp(),
        ),
    )[0]


def _transcript_segments(db: Session, transcript_id: UUID) -> list[Segment]:
    return db.execute(
        select(Segment)
        .where(Segment.transcript_id == transcript_id)
        .order_by(Segment.position.asc())
    ).scalars().all()


def _annotation_response(
    annotation: ObjectModelAnnotation,
    clips_by_uuid: dict[UUID, Clip],
) -> PublicEvidenceAnnotationResponse:
    return PublicEvidenceAnnotationResponse(
        id=_normalized_nonempty_string(annotation.website_annotation_id, "Published annotation not found for this object"),
        title=annotation.title,
        description=_optional_nonempty_string(annotation.description),
        start_ms=annotation.start_ms,
        end_ms=annotation.end_ms,
        point_x=float(annotation.point_x),
        point_y=float(annotation.point_y),
        point_z=float(annotation.point_z),
        normal_x=float(annotation.normal_x) if annotation.normal_x is not None else None,
        normal_y=float(annotation.normal_y) if annotation.normal_y is not None else None,
        normal_z=float(annotation.normal_z) if annotation.normal_z is not None else None,
        camera=_camera_payload(annotation.camera_json),
        related_clip_ids=[
            _normalized_nonempty_string(clip.website_clip_id, "Published clip not found for this object")
            for clip in _annotation_related_clips(annotation, clips_by_uuid)
            if clip.website_clip_id is not None
        ],
    )


def _clip_summary_response(object_name: str, clip: Clip) -> PublicEvidenceClipSummaryResponse:
    return PublicEvidenceClipSummaryResponse(
        id=_normalized_nonempty_string(clip.website_clip_id, "Published clip not found for this object"),
        title=_clip_title(object_name, clip),
        start_ms=clip.start_ms,
        end_ms=clip.end_ms,
    )


def _clip_detail_response(object_name: str, clip: Clip) -> PublicEvidenceClipDetailResponse:
    stream_url = _clip_stream_url(clip)
    poster_url = _clip_poster_url(clip)
    if stream_url is None or poster_url is None:
        raise PublicEvidenceNotFoundError("Published clip media is missing")
    return PublicEvidenceClipDetailResponse(
        id=_normalized_nonempty_string(clip.website_clip_id, "Published clip not found for this object"),
        title=_clip_title(object_name, clip),
        start_ms=clip.start_ms,
        end_ms=clip.end_ms,
        stream_url=stream_url,
        poster_url=poster_url,
        transcript_excerpt=_clip_transcript_excerpt(clip),
    )


def _camera_payload(camera_payload: dict | None) -> CameraViewPayload | None:
    if not isinstance(camera_payload, dict):
        return None
    position = camera_payload.get("position")
    target = camera_payload.get("target")
    if not isinstance(position, (list, tuple)) or not isinstance(target, (list, tuple)):
        return None
    try:
        return CameraViewPayload(
            position=[float(position[0]), float(position[1]), float(position[2])],
            target=[float(target[0]), float(target[1]), float(target[2])],
        )
    except (IndexError, TypeError, ValueError):
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


def _unique_annotation_for_clip(
    clip: Clip,
    annotations: list[ObjectModelAnnotation],
    clips_by_uuid: dict[UUID, Clip],
) -> ObjectModelAnnotation | None:
    matching_annotations = [
        annotation
        for annotation in annotations
        if any(related_clip.id == clip.id for related_clip in _annotation_related_clips(annotation, clips_by_uuid))
    ]

    if len(matching_annotations) == 1:
        return matching_annotations[0]

    return None


def _public_summary(object_row: Object) -> str | None:
    metadata = object_row.metadata_json if isinstance(object_row.metadata_json, dict) else {}
    raw_value = metadata.get("public_standfirst")
    if isinstance(raw_value, str):
        trimmed = raw_value.strip()
        if trimmed:
            return trimmed

    if isinstance(object_row.description, str):
        trimmed_description = object_row.description.strip()
        if trimmed_description:
            return trimmed_description

    return None


def _page_title(object_name: str, resolved: ResolvedEvidenceFocus) -> str:
    if resolved.selected_annotation is not None:
        return resolved.selected_annotation.title
    if resolved.selected_clip is not None:
        return _clip_title(object_name, resolved.selected_clip)
    return object_name


def _page_summary(object_summary: str | None, resolved: ResolvedEvidenceFocus) -> str | None:
    if resolved.selected_annotation is not None:
        annotation_summary = _optional_nonempty_string(resolved.selected_annotation.description)
        if annotation_summary:
            return annotation_summary
    return object_summary


def _citation_attribution_response(
    db: Session,
    resolved: ResolvedEvidenceFocus,
) -> PublicEvidenceCitationAttributionResponse | None:
    bands = _citation_attribution_bands(db, resolved.selected_video)
    payload = _citation_attribution_payload_for_focus(bands, resolved.start_ms)
    if payload is None:
        return None

    return PublicEvidenceCitationAttributionResponse(
        **payload,
    )


def _citation_attribution_timeline_response(
    db: Session,
    video: Video,
) -> list[PublicEvidenceCitationAttributionTimelineEntryResponse]:
    bands = _citation_attribution_bands(db, video)
    return [
        PublicEvidenceCitationAttributionTimelineEntryResponse(
            video_id=video.stable_video_id,
            start_ms=int(band["start_ms"]),
            end_ms=band["end_ms"],
            **band["payload"],
        )
        for band in bands
    ]


def _suppressed_gap_payload() -> dict[str, object]:
    return {
        "speaker_label": None,
        "session_date": None,
        "session_date_text": None,
        "session_date_precision": "unknown",
        "attribution_mode": "unavailable",
    }


def _citation_attribution_bands(
    db: Session,
    video: Video,
) -> list[dict[str, object]]:
    observations = _public_title_card_observations_for_video(db, video.id)
    if not observations:
        return []

    first_eligible_timestamp_ms = max(0, int(observations[0].timestamp_ms))

    # Infer the sampling cadence from the minimum gap between consecutive eligible
    # observations.  Any inter-observation gap larger than one cadence step means
    # one or more sampled timestamps were suppressed/failed and must not inherit an
    # adjacent speaker label.
    cadence_ms = 5000
    if len(observations) >= 2:
        min_gap = min(
            max(1, int(observations[i + 1].timestamp_ms) - int(observations[i].timestamp_ms))
            for i in range(len(observations) - 1)
        )
        cadence_ms = min_gap

    leading_context = _leading_public_title_card_context_observation(
        db,
        video_id=video.id,
        before_timestamp_ms=first_eligible_timestamp_ms,
    )

    # Build a flat sequence of items: named observation groups or suppressed-gap
    # markers.  A gap marker is inserted wherever consecutive observations are
    # separated by more than one cadence step (missing sampled timestamps).
    items: list[dict[str, object]] = []

    if leading_context is not None:
        leading_payload = _citation_attribution_payload(leading_context)
        if _citation_attribution_payload_needs_date_context(leading_payload):
            derived_leading_date_context = _derived_public_date_context_payload(observations)
            if derived_leading_date_context is not None:
                leading_payload = {
                    **leading_payload,
                    **derived_leading_date_context,
                }
        items.append(
            {
                "payload": leading_payload,
                "first_timestamp_ms": max(0, int(leading_context.timestamp_ms)),
                "last_timestamp_ms": max(0, int(leading_context.timestamp_ms)),
                "leading_context": True,
                "is_gap": False,
            }
        )
    elif first_eligible_timestamp_ms > 0:
        # No DB row exists before the first eligible observation and no leading-
        # context card is present.  Insert a suppressed-gap placeholder so seeks
        # before the first named row return no-speaker instead of inheriting the
        # adjacent named speaker via midpoint banding.
        items.append(
            {
                "payload": _suppressed_gap_payload(),
                "first_timestamp_ms": 0,
                "last_timestamp_ms": first_eligible_timestamp_ms,
                "leading_context": False,
                "is_gap": True,
            }
        )

    for i, observation in enumerate(observations):
        payload = _citation_attribution_payload(observation)
        timestamp_ms = max(0, int(observation.timestamp_ms))

        if i > 0:
            prev_ts = max(0, int(observations[i - 1].timestamp_ms))
            inter_gap = timestamp_ms - prev_ts
            if inter_gap > cadence_ms:
                # One or more sampled timestamps are absent between prev_ts and
                # timestamp_ms.  Close the current group with a gap marker, then
                # open a fresh group for the current observation so it is never
                # considered a continuation of the previous group.
                items.append(
                    {
                        "payload": _suppressed_gap_payload(),
                        "first_timestamp_ms": prev_ts,
                        "last_timestamp_ms": timestamp_ms,
                        "leading_context": False,
                        "is_gap": True,
                    }
                )
                items.append(
                    {
                        "payload": payload,
                        "first_timestamp_ms": timestamp_ms,
                        "last_timestamp_ms": timestamp_ms,
                        "leading_context": False,
                        "is_gap": False,
                    }
                )
                continue

        if (
            items
            and not items[-1].get("is_gap")
            and not items[-1].get("leading_context")
            and items[-1]["payload"] == payload
        ):
            items[-1]["last_timestamp_ms"] = timestamp_ms
        else:
            items.append(
                {
                    "payload": payload,
                    "first_timestamp_ms": timestamp_ms,
                    "last_timestamp_ms": timestamp_ms,
                    "leading_context": False,
                    "is_gap": False,
                }
            )

    # Convert items to time-contiguous bands.
    bands: list[dict[str, object]] = []
    for index, item in enumerate(items):
        first_timestamp_ms = int(item["first_timestamp_ms"])
        last_timestamp_ms = int(item["last_timestamp_ms"])
        end_ms: int | None = None

        # Compute start_ms based on the relationship with the predecessor.
        if index == 0:
            start_ms = 0
        elif items[index - 1].get("leading_context"):
            # Exact boundary: leading-context band ends where the first eligible
            # observation begins.
            start_ms = first_timestamp_ms
        elif items[index - 1].get("is_gap"):
            # Continue directly from the end of the preceding gap band.
            prev_band_end = bands[-1]["end_ms"]
            start_ms = int(prev_band_end) if prev_band_end is not None else first_timestamp_ms
        elif item.get("is_gap"):
            # This item is a gap; it starts half a cadence step past the
            # previous group's last observation.
            prev_last = int(items[index - 1]["last_timestamp_ms"])
            start_ms = prev_last + cadence_ms // 2
        else:
            # Normal case: midpoint between the previous group's last timestamp
            # and this group's first timestamp.
            prev_last = int(items[index - 1]["last_timestamp_ms"])
            start_ms = max(0, (prev_last + first_timestamp_ms) // 2)

        # Compute end_ms based on the relationship with the successor.
        if index + 1 < len(items):
            next_item = items[index + 1]
            next_first = int(next_item["first_timestamp_ms"])
            if item.get("leading_context"):
                end_ms = max(start_ms + 1, next_first)
            elif item.get("is_gap"):
                # For inter-speaker gaps, close the gap at prev_last + cadence_ms so
                # the next named speaker begins at the PM-aligned cadence boundary.
                # Intra-speaker and null gaps keep the original half-cadence formula.
                prev_obs_item = items[index - 1] if index > 0 else None
                prev_speaker = (
                    prev_obs_item["payload"].get("speaker_label")
                    if prev_obs_item is not None
                    and not prev_obs_item.get("is_gap")
                    and not prev_obs_item.get("leading_context")
                    else None
                )
                next_speaker = (
                    next_item["payload"].get("speaker_label")
                    if not next_item.get("is_gap")
                    else None
                )
                if prev_speaker and next_speaker and prev_speaker != next_speaker:
                    end_ms = max(start_ms + 1, int(prev_obs_item["last_timestamp_ms"]) + cadence_ms)
                else:
                    end_ms = max(start_ms + 1, next_first - cadence_ms // 2)
            elif next_item.get("is_gap"):
                # Named band ends half a cadence step past its last observation
                # so the subsequent gap band can open there.
                end_ms = max(start_ms + 1, last_timestamp_ms + cadence_ms // 2)
            else:
                end_ms = max(start_ms + 1, (last_timestamp_ms + next_first) // 2)

        bands.append(
            {
                "payload": item["payload"],
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )

    return bands


def _citation_attribution_payload_for_focus(
    bands: list[dict[str, object]],
    focus_ms: int,
) -> dict[str, object] | None:
    if not bands:
        return None

    normalized_focus_ms = max(0, int(focus_ms))
    for band in bands:
        start_ms = int(band["start_ms"])
        end_ms = band["end_ms"]
        if normalized_focus_ms < start_ms:
            continue
        if end_ms is not None and normalized_focus_ms >= int(end_ms):
            continue
        return dict(band["payload"])

    nearest_band = min(
        bands,
        key=lambda band: abs(int(band["start_ms"]) - normalized_focus_ms),
    )
    return dict(nearest_band["payload"])


def _citation_attribution_payload_needs_date_context(payload: dict[str, object]) -> bool:
    session_date_text = payload.get("session_date_text")
    return payload.get("session_date") is None and _optional_nonempty_string(
        session_date_text if isinstance(session_date_text, str) else None
    ) is None


def _derived_public_date_context_payload(observations: list[TitleCardObservation]) -> dict[str, object] | None:
    exact_dates = sorted({observation.session_date for observation in observations if observation.session_date is not None})
    if len(exact_dates) == 1:
        exact_date = exact_dates[0]
        session_date_text = next(
            (
                _optional_nonempty_string(observation.session_date_text)
                for observation in observations
                if observation.session_date == exact_date
                and _optional_nonempty_string(observation.session_date_text) is not None
            ),
            None,
        ) or _format_public_session_date(exact_date)
        return {
            "speaker_label": None,
            "session_date": exact_date,
            "session_date_text": session_date_text,
            "session_date_precision": "day",
            "attribution_mode": "unavailable",
        }

    if len(exact_dates) > 1:
        return {
            "speaker_label": None,
            "session_date": None,
            "session_date_text": f"{_format_public_session_date(exact_dates[0])} - {_format_public_session_date(exact_dates[-1])}",
            "session_date_precision": "text",
            "attribution_mode": "unavailable",
        }

    distinct_date_texts: list[str] = []
    for observation in observations:
        session_date_text = _optional_nonempty_string(observation.session_date_text)
        if session_date_text is None or session_date_text in distinct_date_texts:
            continue
        distinct_date_texts.append(session_date_text)

    if not distinct_date_texts:
        return None

    if len(distinct_date_texts) == 1:
        derived_date_text = distinct_date_texts[0]
    else:
        derived_date_text = f"{distinct_date_texts[0]} - {distinct_date_texts[-1]}"

    return {
        "speaker_label": None,
        "session_date": None,
        "session_date_text": derived_date_text,
        "session_date_precision": "text",
        "attribution_mode": "unavailable",
    }


def _format_public_session_date(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _citation_attribution_payload(observation: TitleCardObservation) -> dict[str, object]:
    speaker_label = _public_title_card_speaker_label(observation)
    return {
        "speaker_label": speaker_label,
        "session_date": observation.session_date,
        "session_date_text": _optional_nonempty_string(observation.session_date_text),
        "session_date_precision": _session_date_precision(observation),
        "attribution_mode": "named" if speaker_label else "unavailable",
    }


def _citation_attribution_timeline_payload(observation: TitleCardObservation) -> dict[str, object]:
    return {
        "speaker_label": _public_title_card_speaker_label(observation),
        "session_date": observation.session_date,
        "session_date_text": _optional_nonempty_string(observation.session_date_text),
    }


def _public_title_card_observations_for_video(
    db: Session,
    video_id: UUID,
) -> list[TitleCardObservation]:
    observations = db.execute(
        select(TitleCardObservation)
        .where(TitleCardObservation.video_id == video_id)
        .order_by(TitleCardObservation.timestamp_ms.asc(), TitleCardObservation.created_at.desc())
    ).scalars().all()

    best_by_timestamp: dict[int, tuple[tuple[int, int, float], TitleCardObservation]] = {}
    for observation in observations:
        if not _title_card_observation_is_public_attribution_eligible(observation):
            continue

        source_priority = _title_card_source_priority(observation.source_kind)
        if source_priority is None:
            continue

        timestamp_ms = max(0, int(observation.timestamp_ms))
        key = (
            _confidence_rank(observation.confidence_session_date),
            source_priority,
            (observation.created_at.timestamp()) if observation.created_at is not None else 0.0,
        )
        current = best_by_timestamp.get(timestamp_ms)
        if current is None or key > current[0]:
            best_by_timestamp[timestamp_ms] = (key, observation)

    return [value[1] for _, value in sorted(best_by_timestamp.items(), key=lambda item: item[0])]


def _leading_public_title_card_context_observation(
    db: Session,
    *,
    video_id: UUID,
    before_timestamp_ms: int,
) -> TitleCardObservation | None:
    if before_timestamp_ms <= 0:
        return None

    observations = db.execute(
        select(TitleCardObservation)
        .where(
            TitleCardObservation.video_id == video_id,
            TitleCardObservation.timestamp_ms < before_timestamp_ms,
        )
        .order_by(TitleCardObservation.timestamp_ms.asc(), TitleCardObservation.created_at.desc())
    ).scalars().all()

    best_by_timestamp: dict[int, tuple[tuple[int, int, float], TitleCardObservation]] = {}
    for observation in observations:
        if not _title_card_observation_is_public_leading_context_eligible(observation):
            continue

        source_priority = _title_card_source_priority(observation.source_kind)
        if source_priority is None:
            continue

        timestamp_ms = max(0, int(observation.timestamp_ms))
        key = (
            _confidence_rank(observation.confidence_session_date),
            source_priority,
            (observation.created_at.timestamp()) if observation.created_at is not None else 0.0,
        )
        current = best_by_timestamp.get(timestamp_ms)
        if current is None or key > current[0]:
            best_by_timestamp[timestamp_ms] = (key, observation)

    if not best_by_timestamp:
        return None

    earliest_timestamp_ms = min(best_by_timestamp)
    return best_by_timestamp[earliest_timestamp_ms][1]


def _nearest_reliable_title_card_observation(
    db: Session,
    *,
    video_id: UUID,
    focus_ms: int,
) -> TitleCardObservation | None:
    observations = _public_title_card_observations_for_video(db, video_id)

    best_observation: TitleCardObservation | None = None
    best_key: tuple[int, int, int, float] | None = None
    for observation in observations:
        source_priority = _title_card_source_priority(observation.source_kind)
        if source_priority is None:
            continue

        key = (
            abs(int(observation.timestamp_ms) - max(0, int(focus_ms))),
            -_confidence_rank(observation.confidence_session_date),
            -source_priority,
            -((observation.created_at.timestamp()) if observation.created_at is not None else 0.0),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_observation = observation

    return best_observation


def _title_card_observation_has_reliable_date(observation: TitleCardObservation) -> bool:
    if not observation.title_card_visible:
        return False

    has_date_value = observation.session_date is not None or _optional_nonempty_string(observation.session_date_text) is not None
    if not has_date_value:
        return False

    return _normalized_confidence_value(observation.confidence_session_date) in _RELIABLE_TITLE_CARD_CONFIDENCES


def _title_card_observation_is_public_attribution_eligible(observation: TitleCardObservation) -> bool:
    if _normalized_title_card_status(observation.status) != "ready":
        return False
    if _title_card_source_priority(observation.source_kind) is None:
        return False
    return _title_card_observation_has_reliable_date(observation)


def _title_card_observation_is_public_leading_context_eligible(observation: TitleCardObservation) -> bool:
    if _normalized_title_card_status(observation.status) != "ready":
        return False
    if _title_card_source_priority(observation.source_kind) is None:
        return False
    return bool(observation.title_card_visible)


def _public_title_card_speaker_label(observation: TitleCardObservation) -> str | None:
    if not _title_card_observation_has_reliable_date(observation):
        return None
    speaker_label = _optional_nonempty_string(observation.public_speaker_label)
    if speaker_label is None:
        return None
    if _normalized_confidence_value(observation.confidence_presenter) not in _RELIABLE_TITLE_CARD_CONFIDENCES:
        return None
    return speaker_label


def _session_date_precision(observation: TitleCardObservation) -> Literal["day", "text", "unknown"]:
    if observation.session_date is not None:
        return "day"
    if _optional_nonempty_string(observation.session_date_text) is not None:
        return "text"
    return "unknown"


def _normalized_confidence_value(value: str | None) -> Literal["high", "medium", "low", "none"] | None:
    normalized = _optional_nonempty_string(value)
    if normalized is None:
        return None

    lowered = normalized.lower()
    if lowered in {"high", "medium", "low", "none"}:
        return lowered
    return None


def _normalized_title_card_status(value: str | None) -> Literal["pending", "ready", "failed"] | None:
    normalized = _optional_nonempty_string(value)
    if normalized is None:
        return None

    lowered = normalized.lower()
    if lowered in {"pending", "ready", "failed"}:
        return lowered
    return None


def _normalized_title_card_source_kind(value: str | None) -> str | None:
    normalized = _optional_nonempty_string(value)
    if normalized is None:
        return None

    lowered = _TITLE_CARD_PUBLIC_SOURCE_ALIASES.get(normalized.lower(), normalized.lower())
    if lowered in _TITLE_CARD_PUBLIC_SOURCE_PRIORITY:
        return lowered
    return None


def _title_card_source_priority(value: str | None) -> int | None:
    normalized = _normalized_title_card_source_kind(value)
    if normalized is None:
        return None
    return _TITLE_CARD_PUBLIC_SOURCE_PRIORITY[normalized]


def _confidence_rank(value: str | None) -> int:
    normalized = _normalized_confidence_value(value)
    return {
        "high": 3,
        "medium": 2,
        "low": 1,
        "none": 0,
    }.get(normalized or "none", 0)


def _canonical_url(website_object_id: str, resolved: ResolvedEvidenceFocus) -> str:
    query: list[tuple[str, str]] = []
    if resolved.source == "clip" and resolved.selected_clip is not None and resolved.selected_clip.website_clip_id:
        query.append(("clip", resolved.selected_clip.website_clip_id))
    elif resolved.source in {"annotation", "default"} and resolved.selected_annotation is not None and resolved.selected_annotation.website_annotation_id:
        query.append(("annotation", resolved.selected_annotation.website_annotation_id))
    else:
        query.append(("video", resolved.selected_video.stable_video_id))
        if resolved.seek_ms > 0:
            query.append(("t", str(resolved.seek_ms)))

    encoded = urlencode(query)
    base_path = f"/evidence/objects/{website_object_id}"
    return f"{base_path}?{encoded}" if encoded else base_path


def _clip_transcript_excerpt(clip: Clip) -> str | None:
    explicit_excerpt = _optional_nonempty_string(clip.transcript_excerpt)
    if explicit_excerpt:
        return explicit_excerpt

    transcript_path = _existing_file(_normalized_path(clip.metadata_json_path))
    if transcript_path is None:
        return None
    trimmed = transcript_path.read_text(encoding="utf-8").strip()
    return trimmed or None


def _positive_duration_ms(value: int | None) -> int | None:
    """Only publish a duration the timeline can actually render.

    A zero or negative stored duration is indistinguishable from "unknown" for a
    scrubber, so it is reported as absent rather than as a zero-length video.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value > 0 else None


def _public_model_url(db: Session, object_id: UUID, model: ObjectModel) -> str | None:
    model_path = _existing_file(_normalized_path(model.storage_path))
    if model_path is None:
        return None
    return f"/api/v1/public/objects/{object_id}/model/file?v={model_cache_token(db, model)}"


def _video_stream_url(video: Video) -> str | None:
    playback_path = video_playback_path(video)
    if playback_path is None or not playback_path.exists() or not playback_path.is_file():
        return None
    return f"/api/v1/public/videos/{video.id}/stream?v={video_cache_token(video)}"


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


def _clip_title(object_name: str, clip: Clip) -> str:
    return f"{object_name} clip {_format_clip_seconds(clip.start_ms)}-{_format_clip_seconds(clip.end_ms)}"


def _format_clip_seconds(value_ms: int) -> str:
    total_seconds = max(0, int(value_ms // 1000))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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


def _normalized_nonempty_string(value: str | None, error_message: str) -> str:
    if not isinstance(value, str):
        raise PublicEvidenceNotFoundError(error_message)
    trimmed = value.strip()
    if not trimmed:
        raise PublicEvidenceNotFoundError(error_message)
    return trimmed


def _optional_nonempty_string(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None
