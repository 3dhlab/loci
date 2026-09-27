from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import (
    AnnotationReviewStatus,
    Clip,
    ClipStatus,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    Project,
    Segment,
    Transcript,
    TranscriptFormat,
    TranscriptSource,
    TranscriptWindow,
    Video,
    VideoStatus,
    VisualWindowDescription,
)
from app.services.ai import AIProviderError, get_embedding_provider
from app.services.corpus_phrase_builder import (
    DENSITY_REFERENCE_WINDOW_COUNT,
    _compute_density,
    _extract_phrases_yake,
    _phrase_is_useful,
    normalize_phrase,
)
from app.services.media_clips import clip_poster_path_for_video
from app.services.media_transcode import transcode_output_path


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return value


def _relative_media_path(path_value: str | Path) -> str:
    path = Path(path_value).resolve()
    media_root = Path(settings.media_root).resolve()
    try:
        return path.relative_to(media_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path is outside media_root: {path}") from exc


def _optional_relative_media_path(path_value: str | Path | None, media_paths: set[str]) -> str | None:
    if not path_value:
        return None

    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None

    relative_path = _relative_media_path(path)
    media_paths.add(relative_path)
    return relative_path


def _serialize_visual_frame_manifest(frame_manifest: dict | None, media_paths: set[str]) -> dict | None:
    manifest = _jsonable(frame_manifest)
    if not isinstance(manifest, dict):
        return manifest

    thumbnail = manifest.get("thumbnail")
    if isinstance(thumbnail, dict):
        thumbnail["path"] = _optional_relative_media_path(thumbnail.get("path"), media_paths)

    serialized_sample_frames: list[dict] = []
    for entry in manifest.get("sample_frames") or []:
        if not isinstance(entry, dict):
            continue

        relative_path = _optional_relative_media_path(entry.get("path"), media_paths)
        if not relative_path:
            continue

        serialized_entry = dict(entry)
        serialized_entry["path"] = relative_path
        serialized_sample_frames.append(serialized_entry)

    manifest["sample_frames"] = serialized_sample_frames
    return manifest


def _preferred_published_transcript(rows: list[Transcript]) -> Transcript | None:
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            0 if row.source == TranscriptSource.MANUAL else 1,
            -(row.updated_at or row.created_at).timestamp(),
        ),
    )[0]


def _default_playlist(annotation: ObjectModelAnnotation) -> list[dict]:
    return [
        {
            "video_id": str(annotation.video_id),
            "clip_id": None,
            "transcript_segment_id": str(annotation.transcript_segment_id) if annotation.transcript_segment_id else None,
            "label": "Clip 1",
            "start_ms": int(annotation.start_ms),
            "end_ms": int(annotation.end_ms),
        }
    ]


def _build_public_corpus_phrase_rows(
    *,
    project_rows: list[Project],
    published_objects: list[Object],
    projected_videos: list[Video],
    projected_transcripts: list[Transcript],
    segment_rows: list[Segment],
    transcript_window_rows: list[TranscriptWindow],
    visual_window_rows: list[VisualWindowDescription],
) -> list[dict]:
    video_project_by_id = {video.id: video.project_id for video in projected_videos}
    transcript_project_by_id = {
        transcript.id: video_project_by_id[transcript.video_id]
        for transcript in projected_transcripts
        if transcript.video_id in video_project_by_id
    }

    reference_vectors_by_project: dict[UUID, list] = defaultdict(list)
    for window in transcript_window_rows:
        project_id = transcript_project_by_id.get(window.transcript_id)
        if project_id is None or window.embedding_vector is None:
            continue
        if len(reference_vectors_by_project[project_id]) >= DENSITY_REFERENCE_WINDOW_COUNT:
            continue
        reference_vectors_by_project[project_id].append(window.embedding_vector)

    records: list[dict] = []
    for segment in segment_rows:
        project_id = transcript_project_by_id.get(segment.transcript_id)
        if project_id is None:
            continue
        for phrase in _extract_phrases_yake(segment.text or "", 5):
            if _phrase_is_useful(phrase):
                records.append({"project_id": project_id, "phrase": phrase, "source": "transcript"})

    for visual in visual_window_rows:
        project_id = video_project_by_id.get(visual.video_id)
        if project_id is None:
            continue
        for phrase in _extract_phrases_yake(visual.description_text or "", 5):
            if _phrase_is_useful(phrase):
                records.append({"project_id": project_id, "phrase": phrase, "source": "visual_description"})

    for obj in published_objects:
        candidate = (obj.name or "").strip()
        if _phrase_is_useful(candidate):
            records.append({"project_id": obj.project_id, "phrase": candidate, "source": "entity"})

    for project in project_rows:
        candidate = (project.name or "").strip()
        if _phrase_is_useful(candidate):
            records.append({"project_id": project.id, "phrase": candidate, "source": "entity"})

    aggregated: dict[tuple[UUID, str, str], dict] = {}
    for record in records:
        normalized = normalize_phrase(record["phrase"])
        if not normalized:
            continue

        key = (record["project_id"], normalized, record["source"])
        if key in aggregated:
            aggregated[key]["source_count"] += 1
            continue

        project_id, _, source = key
        aggregated[key] = {
            "id": str(uuid5(NAMESPACE_URL, f"public-corpus-phrase:{project_id}:{source}:{normalized}")),
            "project_id": str(project_id),
            "phrase_text": record["phrase"].strip(),
            "normalized_phrase": normalized,
            "source": source,
            "source_count": 1,
        }

    if not aggregated:
        return []

    phrase_rows = list(aggregated.values())
    texts = [row["phrase_text"] for row in phrase_rows]
    try:
        provider = get_embedding_provider()
        embeddings = list(provider.embed_texts(texts).vectors)
    except AIProviderError:
        embeddings = [None] * len(texts)

    generated_at = _iso(datetime.now(timezone.utc))
    for row, embedding in zip(phrase_rows, embeddings):
        project_id = UUID(row["project_id"])
        row["embedding_vector"] = _jsonable(list(embedding) if embedding is not None else None)
        row["corpus_density"] = float(_compute_density(embedding, reference_vectors_by_project.get(project_id, [])))
        row["created_at"] = generated_at
        row["updated_at"] = generated_at

    return sorted(
        phrase_rows,
        key=lambda item: (item["project_id"], item["source"], item["normalized_phrase"]),
    )


def build_projection() -> tuple[dict, list[str]]:
    media_paths: set[str] = set()

    with SessionLocal() as db:
        published_objects = db.execute(
            select(Object).where(Object.is_published.is_(True)).order_by(Object.created_at.asc())
        ).scalars().all()
        published_object_ids = {row.id for row in published_objects}

        project_rows = db.execute(
            select(Project).where(Project.id.in_({row.project_id for row in published_objects}))
        ).scalars().all() if published_objects else []
        project_by_id = {row.id: row for row in project_rows}

        candidate_videos = db.execute(
            select(Video)
            .where(
                Video.object_id.in_(published_object_ids) if published_object_ids else False,
                Video.is_published.is_(True),
            )
            .order_by(Video.created_at.asc())
        ).scalars().all() if published_objects else []

        projected_videos: list[Video] = []
        projected_video_ids: set[UUID] = set()
        for video in candidate_videos:
            if video.status != VideoStatus.READY:
                continue
            playback_path = transcode_output_path(str(video.id))
            if not playback_path.exists() or not playback_path.is_file():
                continue
            media_paths.add(_relative_media_path(playback_path))
            projected_videos.append(video)
            projected_video_ids.add(video.id)

        transcript_rows = db.execute(
            select(Transcript)
            .where(
                Transcript.video_id.in_(projected_video_ids) if projected_video_ids else False,
                Transcript.is_published.is_(True),
            )
            .order_by(Transcript.updated_at.desc())
        ).scalars().all() if projected_video_ids else []
        transcripts_by_video: dict[UUID, list[Transcript]] = defaultdict(list)
        for transcript in transcript_rows:
            transcripts_by_video[transcript.video_id].append(transcript)

        projected_transcripts: list[Transcript] = []
        projected_transcript_ids: set[UUID] = set()
        for video in projected_videos:
            transcript = _preferred_published_transcript(transcripts_by_video.get(video.id, []))
            if transcript is None:
                continue
            projected_transcripts.append(transcript)
            projected_transcript_ids.add(transcript.id)

        segment_rows = db.execute(
            select(Segment)
            .where(Segment.transcript_id.in_(projected_transcript_ids) if projected_transcript_ids else False)
            .order_by(Segment.transcript_id.asc(), Segment.position.asc())
        ).scalars().all() if projected_transcript_ids else []
        projected_segment_ids = {row.id for row in segment_rows}

        transcript_window_rows = db.execute(
            select(TranscriptWindow)
            .where(TranscriptWindow.transcript_id.in_(projected_transcript_ids) if projected_transcript_ids else False)
            .order_by(TranscriptWindow.transcript_id.asc(), TranscriptWindow.window_index.asc())
        ).scalars().all() if projected_transcript_ids else []
        projected_transcript_window_ids = {row.id for row in transcript_window_rows}

        visual_window_rows = db.execute(
            select(VisualWindowDescription)
            .where(
                VisualWindowDescription.transcript_window_id.in_(projected_transcript_window_ids)
                if projected_transcript_window_ids else False
            )
            .order_by(VisualWindowDescription.transcript_id.asc(), VisualWindowDescription.start_ms.asc())
        ).scalars().all() if projected_transcript_window_ids else []

        model_rows = db.execute(
            select(ObjectModel)
            .where(
                ObjectModel.object_id.in_(published_object_ids) if published_object_ids else False,
                ObjectModel.is_published.is_(True),
            )
            .order_by(ObjectModel.created_at.asc())
        ).scalars().all() if published_object_ids else []

        projected_models: list[ObjectModel] = []
        projected_model_ids: set[UUID] = set()
        for model in model_rows:
            model_path = Path(model.storage_path)
            if not model_path.exists() or not model_path.is_file():
                continue
            media_paths.add(_relative_media_path(model_path))
            poster_path = Path(model.public_poster_path) if model.public_poster_path else None
            if poster_path is not None and poster_path.exists() and poster_path.is_file():
                media_paths.add(_relative_media_path(poster_path))
            projected_models.append(model)
            projected_model_ids.add(model.id)

        clip_rows = db.execute(
            select(Clip)
            .where(
                Clip.video_id.in_(projected_video_ids) if projected_video_ids else False,
                Clip.status == ClipStatus.COMPLETE,
                Clip.website_clip_id.is_not(None),
            )
            .order_by(Clip.created_at.asc(), Clip.id.asc())
        ).scalars().all() if projected_video_ids else []

        projected_clips: list[Clip] = []
        projected_clip_ids: set[UUID] = set()
        for clip in clip_rows:
            if not clip.output_mp4_path:
                continue
            clip_path = Path(clip.output_mp4_path)
            if not clip_path.exists() or not clip_path.is_file():
                continue
            poster_path = clip_poster_path_for_video(clip_path)
            if not poster_path.exists() or not poster_path.is_file():
                continue
            media_paths.add(_relative_media_path(clip_path))
            media_paths.add(_relative_media_path(poster_path))
            transcript_path = Path(clip.metadata_json_path) if clip.metadata_json_path else None
            if transcript_path is not None and transcript_path.exists() and transcript_path.is_file():
                media_paths.add(_relative_media_path(transcript_path))
            projected_clips.append(clip)
            projected_clip_ids.add(clip.id)

        annotation_rows = db.execute(
            select(ObjectModelAnnotation)
            .where(
                ObjectModelAnnotation.object_id.in_(published_object_ids) if published_object_ids else False,
                ObjectModelAnnotation.is_published.is_(True),
            )
            .order_by(ObjectModelAnnotation.created_at.asc())
        ).scalars().all() if published_object_ids else []

        projected_annotations: list[dict] = []
        for annotation in annotation_rows:
            if annotation.object_model_id not in projected_model_ids:
                continue
            if annotation.video_id not in projected_video_ids:
                continue
            if annotation.transcript_segment_id and annotation.transcript_segment_id not in projected_segment_ids:
                continue

            raw_playlist = annotation.playlist_json or _default_playlist(annotation)
            playlist: list[dict] = []
            valid = True
            for index, entry in enumerate(raw_playlist, start=1):
                video_id = UUID(str(entry["video_id"]))
                if video_id not in projected_video_ids:
                    valid = False
                    break
                clip_id = UUID(str(entry["clip_id"])) if entry.get("clip_id") else None
                if clip_id is not None and clip_id not in projected_clip_ids:
                    clip_id = None
                transcript_segment_id = (
                    UUID(str(entry["transcript_segment_id"])) if entry.get("transcript_segment_id") else None
                )
                if transcript_segment_id and transcript_segment_id not in projected_segment_ids:
                    valid = False
                    break
                playlist.append(
                    {
                        "video_id": str(video_id),
                        "clip_id": str(clip_id) if clip_id else None,
                        "transcript_segment_id": str(transcript_segment_id) if transcript_segment_id else None,
                        "label": (entry.get("label") or f"Clip {index}").strip(),
                        "start_ms": int(entry["start_ms"]),
                        "end_ms": int(entry["end_ms"]),
                    }
                )
            if not valid:
                continue

            projected_annotations.append(
                {
                    "id": str(annotation.id),
                    "object_model_id": str(annotation.object_model_id),
                    "object_id": str(annotation.object_id),
                    "video_id": str(annotation.video_id),
                    "clip_id": str(annotation.clip_id) if annotation.clip_id in projected_clip_ids else None,
                    "transcript_segment_id": str(annotation.transcript_segment_id) if annotation.transcript_segment_id else None,
                    "title": annotation.title,
                    "description": annotation.description,
                    "point_x": float(annotation.point_x),
                    "point_y": float(annotation.point_y),
                    "point_z": float(annotation.point_z),
                    "normal_x": float(annotation.normal_x) if annotation.normal_x is not None else None,
                    "normal_y": float(annotation.normal_y) if annotation.normal_y is not None else None,
                    "normal_z": float(annotation.normal_z) if annotation.normal_z is not None else None,
                    "camera_json": _jsonable(annotation.camera_json),
                    "playlist_json": _jsonable(playlist),
                    "start_ms": int(annotation.start_ms),
                    "end_ms": int(annotation.end_ms),
                    "review_status": AnnotationReviewStatus.ACTIVE.value,
                    "is_published": True,
                    "website_annotation_id": annotation.website_annotation_id,
                    "website_relations_json": _jsonable(annotation.website_relations_json),
                    "model_revision_created_against": int(annotation.model_revision_created_against),
                    "created_at": _iso(annotation.created_at),
                    "updated_at": _iso(annotation.updated_at),
                }
            )

        manifest = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "embedding_contract": {
                "provider": settings.embedding_provider,
                "local_embedding_model": settings.local_embedding_model,
                "embedding_vector_dimensions": settings.embedding_vector_dimensions,
            },
            "projects": [
                {
                    "id": str(project.id),
                    "name": project.name,
                    "description": project.description,
                    "website_slug": project.website_slug,
                    "created_at": _iso(project.created_at),
                    "updated_at": _iso(project.updated_at),
                }
                for project in project_rows
            ],
            "objects": [
                {
                    "id": str(obj.id),
                    "project_id": str(obj.project_id),
                    "name": obj.name,
                    "description": obj.description,
                    "external_url": obj.external_url,
                    "website_object_id": obj.website_object_id,
                    "metadata_json": _jsonable(obj.metadata_json),
                    "is_published": True,
                    "is_embed_ready": bool(obj.is_embed_ready),
                    "created_at": _iso(obj.created_at),
                    "updated_at": _iso(obj.updated_at),
                }
                for obj in published_objects
            ],
            "videos": [
                {
                    "id": str(video.id),
                    "project_id": str(video.project_id),
                    "object_id": str(video.object_id) if video.object_id else None,
                    "stable_video_id": video.stable_video_id,
                    "title": video.title,
                    "original_filename": video.original_filename,
                    "public_media_relpath": f"transcoded/{video.id}.mp4",
                    "sha256_checksum": video.sha256_checksum,
                    "duration_ms": video.duration_ms,
                    "status": VideoStatus.READY.value,
                    "is_published": True,
                    "created_at": _iso(video.created_at),
                    "updated_at": _iso(video.updated_at),
                }
                for video in projected_videos
            ],
            "transcripts": [
                {
                    "id": str(transcript.id),
                    "video_id": str(transcript.video_id),
                    "title": transcript.title,
                    "source": transcript.source.value,
                    "language": transcript.language,
                    "format": transcript.format.value,
                    "raw_text": transcript.raw_text,
                    "is_published": True,
                    "created_at": _iso(transcript.created_at),
                    "updated_at": _iso(transcript.updated_at),
                }
                for transcript in projected_transcripts
            ],
            "segments": [
                {
                    "id": str(segment.id),
                    "transcript_id": str(segment.transcript_id),
                    "position": int(segment.position),
                    "start_ms": int(segment.start_ms),
                    "end_ms": int(segment.end_ms),
                    "text": segment.text,
                    "embedding_vector": _jsonable(segment.embedding_vector),
                    "created_at": _iso(segment.created_at),
                }
                for segment in segment_rows
            ],
            "transcript_windows": [
                {
                    "id": str(window.id),
                    "transcript_id": str(window.transcript_id),
                    "window_index": int(window.window_index),
                    "start_position": int(window.start_position),
                    "end_position": int(window.end_position),
                    "start_ms": int(window.start_ms),
                    "end_ms": int(window.end_ms),
                    "text": window.text,
                    "embedding_vector": _jsonable(window.embedding_vector),
                    "created_at": _iso(window.created_at),
                }
                for window in transcript_window_rows
            ],
            "visual_window_descriptions": [
                {
                    "id": str(visual.id),
                    "transcript_window_id": str(visual.transcript_window_id),
                    "transcript_id": str(visual.transcript_id),
                    "video_id": str(visual.video_id),
                    "start_ms": int(visual.start_ms),
                    "end_ms": int(visual.end_ms),
                    "description_text": visual.description_text,
                    "embedding_vector": _jsonable(visual.embedding_vector),
                    "thumbnail_relpath": _optional_relative_media_path(visual.thumbnail_path, media_paths),
                    "frame_manifest_json": _serialize_visual_frame_manifest(visual.frame_manifest_json, media_paths),
                    "generator_provider": visual.generator_provider,
                    "generator_model": visual.generator_model,
                    "embedding_provider": visual.embedding_provider,
                    "embedding_model": visual.embedding_model,
                    "status": visual.status,
                    "created_at": _iso(visual.created_at),
                    "updated_at": _iso(visual.updated_at),
                }
                for visual in visual_window_rows
            ],
            "object_models": [
                {
                    "id": str(model.id),
                    "object_id": str(model.object_id),
                    "public_media_relpath": _relative_media_path(model.storage_path),
                    "original_filename": model.original_filename,
                    "mime_type": model.mime_type,
                    "sha256_checksum": model.sha256_checksum,
                    "file_size_bytes": int(model.file_size_bytes),
                    "revision_number": int(model.revision_number),
                    "position_x": float(model.position_x),
                    "position_y": float(model.position_y),
                    "position_z": float(model.position_z),
                    "rotation_x": float(model.rotation_x),
                    "rotation_y": float(model.rotation_y),
                    "rotation_z": float(model.rotation_z),
                    "default_camera_json": _jsonable(model.default_camera_json),
                    "public_poster_relpath": _relative_media_path(model.public_poster_path) if model.public_poster_path else None,
                    "is_published": True,
                    "created_at": _iso(model.created_at),
                    "updated_at": _iso(model.updated_at),
                }
                for model in projected_models
            ],
            "clips": [
                {
                    "id": str(clip.id),
                    "video_id": str(clip.video_id),
                    "project_id": str(clip.project_id),
                    "start_ms": int(clip.start_ms),
                    "end_ms": int(clip.end_ms),
                    "public_media_relpath": _relative_media_path(clip.output_mp4_path),
                    "public_poster_relpath": _relative_media_path(clip_poster_path_for_video(clip.output_mp4_path)),
                    "public_transcript_relpath": _relative_media_path(clip.metadata_json_path) if clip.metadata_json_path else None,
                    "website_clip_id": clip.website_clip_id,
                    "transcript_excerpt": clip.transcript_excerpt,
                    "citation_text": clip.citation_text,
                    "status": ClipStatus.COMPLETE.value,
                    "created_at": _iso(clip.created_at),
                    "updated_at": _iso(clip.updated_at),
                }
                for clip in projected_clips
            ],
            "corpus_phrases": _build_public_corpus_phrase_rows(
                project_rows=project_rows,
                published_objects=published_objects,
                projected_videos=projected_videos,
                projected_transcripts=projected_transcripts,
                segment_rows=segment_rows,
                transcript_window_rows=transcript_window_rows,
                visual_window_rows=visual_window_rows,
            ),
            "object_model_annotations": projected_annotations,
            "media_paths": sorted(media_paths),
        }

    return manifest, sorted(media_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the published object-package projection for semantic_public.")
    parser.add_argument("--output", required=True, help="Path to write the projection manifest JSON.")
    parser.add_argument("--media-list", required=True, help="Path to write newline-delimited relative media paths.")
    args = parser.parse_args()

    manifest, media_paths = build_projection()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    media_list_path = Path(args.media_list)
    media_list_path.parent.mkdir(parents=True, exist_ok=True)
    media_list_path.write_text("\n".join(media_paths) + ("\n" if media_paths else ""), encoding="utf-8")

    print(
        json.dumps(
            {
                "manifest": str(output_path),
                "media_list": str(media_list_path),
                "project_count": len(manifest["projects"]),
                "object_count": len(manifest["objects"]),
                "video_count": len(manifest["videos"]),
                "transcript_count": len(manifest["transcripts"]),
                "segment_count": len(manifest["segments"]),
                "transcript_window_count": len(manifest["transcript_windows"]),
                "corpus_phrase_count": len(manifest.get("corpus_phrases", [])),
                "model_count": len(manifest["object_models"]),
                "clip_count": len(manifest["clips"]),
                "annotation_count": len(manifest["object_model_annotations"]),
                "media_file_count": len(media_paths),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
