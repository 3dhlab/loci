from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import (
    Clip,
    ClipStatus,
    ModelProvider,
    ModelRun,
    Project,
    Segment,
    Transcript,
    TranscriptFormat,
    TranscriptSource,
    TranscriptWindow,
    VisualWindowDescription,
    Video,
    VideoStatus,
)
from app.services.ai import AIProviderError, get_embedding_provider, get_local_embedding_provider, get_openai_provider, get_vision_provider
from app.services.media_transcode import (
    MediaProbeError,
    MediaReviewRequired,
    MediaValidationError,
    PlaybackArtifactValidation,
    probe_media,
    public_playback_transcode_command,
    require_public_playback_ffmpeg_runtime,
    run_transcode_process,
    transcode_output_dir,
    transcode_temp_output_path,
    validate_public_playback_artifact,
    versioned_transcode_output_path,
    video_playback_path,
)
from app.services.media_windows import (
    visual_window_output_dir,
    visual_window_sample_path,
    visual_window_thumbnail_path,
    visual_window_transcript_dir,
)
from app.services.transcript_windows import build_transcript_windows
from app.services.transcript_parser import ParsedSegment, parse_transcript
from app.services.title_card_ocr import (
    TitleCardImportBatch,
    TitleCardObservationDraft,
    build_title_card_ocr_prompt,
    import_title_card_observation_batch,
    load_title_card_import_batch,
    title_card_sample_timestamps,
)

from app.services.media_clips import (
    clip_output_dir,
    clip_output_path,
    clip_poster_path,
    clip_temp_output_path,
    clip_transcript_path,
)
logger = logging.getLogger(__name__)
VISUAL_WINDOW_SAMPLE_POSITIONS = (0.0, 0.25, 0.5, 0.75, 1.0)


@contextmanager
def _exclusive_transcode_lock(video_id: str):
    """Serialize one video's transcodes across all workers without stale locks."""
    lock_dir = transcode_output_dir() / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / f"{video_id}.lock").open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _set_video_playback_identity(
    video: Video,
    artifact: PlaybackArtifactValidation,
    *,
    storage_key: str,
) -> None:
    video.playback_recipe_version = artifact.recipe_revision
    video.playback_sha256_checksum = artifact.sha256
    video.playback_file_size_bytes = artifact.size_bytes
    video.playback_color_policy = artifact.color_policy
    video.playback_storage_key = storage_key


def _redact_transcode_diagnostics(text: str, *paths: Path) -> str:
    redacted = text
    for path in paths:
        redacted = redacted.replace(str(path), f"[{path.suffix.lstrip('.') or 'media'}]")
    return redacted[-4000:]


def _publish_validated_playback(
    db,
    video: Video,
    *,
    artifact: PlaybackArtifactValidation,
    temp_output_path: Path,
    publication_token: str,
) -> Path:
    """Publish a new immutable file and select it in the READY transaction."""

    output_path = versioned_transcode_output_path(
        str(video.id),
        artifact.sha256,
        publication_token,
    )
    publication_committed = False

    try:
        # Link into a never-reused version key with EEXIST semantics. Readers
        # retain prior immutable inodes, and an extraordinary token collision
        # fails closed instead of overwriting published bytes.
        os.link(temp_output_path, output_path)
        temp_output_path.unlink()
        _set_video_playback_identity(video, artifact, storage_key=output_path.name)
        _set_video_transcode_state(
            db,
            video,
            status=VideoStatus.READY,
            progress_pct=100,
            stage="ready",
        )
        publication_committed = True
    except BaseException:
        db.rollback()
        # A database transport error can be ambiguous after the server accepted
        # the commit. Retain this unique immutable file on every commit failure;
        # a later gated orphan collector may remove keys that no row references.
        raise
    if not publication_committed:  # pragma: no cover - defensive invariant
        raise MediaValidationError("playback_publication_not_committed")
    return output_path


def _set_video_transcode_state(
    db,
    video: Video,
    *,
    status: VideoStatus | None = None,
    progress_pct: int | None = None,
    stage: str | None = None,
    started_at: datetime | None = None,
) -> None:
    if status is not None:
        video.status = status
    if progress_pct is not None:
        video.transcode_progress_pct = max(0, min(100, int(progress_pct)))
    if stage is not None:
        video.transcode_stage = stage
    if started_at is not None:
        video.transcode_started_at = started_at
    db.add(video)
    db.commit()


def _capture_video_delivery_state(video: Video) -> dict[str, object]:
    """Capture the public playback state that a replacement attempt must preserve."""

    return {
        "status": video.status,
        "transcode_progress_pct": video.transcode_progress_pct,
        "transcode_stage": video.transcode_stage,
        "transcode_started_at": video.transcode_started_at,
        "playback_recipe_version": video.playback_recipe_version,
        "playback_sha256_checksum": video.playback_sha256_checksum,
        "playback_file_size_bytes": video.playback_file_size_bytes,
        "playback_color_policy": video.playback_color_policy,
        "playback_storage_key": getattr(video, "playback_storage_key", None),
    }


def _finish_failed_transcode_attempt(
    db,
    video: Video,
    *,
    prior_delivery_state: dict[str, object],
    preserve_ready_playback: bool,
    failed_stage: str,
) -> None:
    """Record a failed first encode or restore an existing public rendition."""

    # A progress or publication commit may be the operation that failed.  Make
    # the session usable before recording the final state.
    db.rollback()
    if preserve_ready_playback:
        for field, value in prior_delivery_state.items():
            setattr(video, field, value)
        db.add(video)
        db.commit()
        return

    _set_video_transcode_state(
        db,
        video,
        status=VideoStatus.FAILED,
        progress_pct=100,
        stage=failed_stage,
    )


def _format_ts(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _resolve_video_input_path(video: Video) -> Path:
    normalized_input = video_playback_path(video)
    return normalized_input if normalized_input is not None and normalized_input.exists() else Path(video.source_path)


def _visual_prompt_for_window(window: TranscriptWindow) -> str:
    template = settings.visual_description_prompt_template
    transcript_excerpt = _truncate_transcript_excerpt(window.text)
    try:
        return template.format(
            start_ms=window.start_ms,
            end_ms=window.end_ms,
            duration_ms=max(0, window.end_ms - window.start_ms),
            transcript_text=transcript_excerpt,
        )
    except KeyError:
        return template


def _truncate_transcript_excerpt(text: str | None) -> str:
    normalized = " ".join((text or "").split())
    if not normalized:
        return "No transcript excerpt available."

    max_chars = max(80, int(settings.visual_description_transcript_char_limit))
    if len(normalized) <= max_chars:
        return normalized

    clipped = normalized[: max_chars + 1]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return f"{clipped.rstrip(' ,;:.')}..."


def _sync_transcript_windows(db, *, transcript_id: UUID, segments: list[Segment]) -> list[TranscriptWindow]:
    drafts = list(build_transcript_windows(segments))
    existing_windows = db.execute(
        select(TranscriptWindow)
        .where(TranscriptWindow.transcript_id == transcript_id)
        .order_by(TranscriptWindow.window_index.asc())
    ).scalars().all()
    existing_by_index = {window.window_index: window for window in existing_windows}

    synced_windows: list[TranscriptWindow] = []
    retained_indexes: set[int] = set()

    for draft in drafts:
        retained_indexes.add(draft.window_index)
        window = existing_by_index.get(draft.window_index)
        if window is None:
            window = TranscriptWindow(
                transcript_id=transcript_id,
                window_index=draft.window_index,
                start_position=draft.start_position,
                end_position=draft.end_position,
                start_ms=draft.start_ms,
                end_ms=draft.end_ms,
                text=draft.text,
            )
        else:
            window.start_position = draft.start_position
            window.end_position = draft.end_position
            window.start_ms = draft.start_ms
            window.end_ms = draft.end_ms
            window.text = draft.text
            window.embedding_vector = None

        db.add(window)
        synced_windows.append(window)

    for window in existing_windows:
        if window.window_index not in retained_indexes:
            db.delete(window)

    db.flush()
    return synced_windows


def _visual_status_counts(db, *, transcript_id: UUID) -> tuple[int, int]:
    ready_count = db.execute(
        select(func.count(VisualWindowDescription.id)).where(
            VisualWindowDescription.transcript_id == transcript_id,
            VisualWindowDescription.status == "ready",
        )
    ).scalar_one()
    failed_count = db.execute(
        select(func.count(VisualWindowDescription.id)).where(
            VisualWindowDescription.transcript_id == transcript_id,
            VisualWindowDescription.status == "failed",
        )
    ).scalar_one()
    return int(ready_count or 0), int(failed_count or 0)


def _window_sample_timestamps(start_ms: int, end_ms: int) -> list[int]:
    duration_ms = max(0, end_ms - start_ms)
    if duration_ms <= 0:
        return [max(0, start_ms) for _ in VISUAL_WINDOW_SAMPLE_POSITIONS]

    return [
        max(0, int(start_ms + round(duration_ms * offset)))
        for offset in VISUAL_WINDOW_SAMPLE_POSITIONS
    ]


def _extract_frame_at_timestamp(source_path: Path, target_path: Path, timestamp_ms: int) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{max(0, timestamp_ms) / 1000.0:.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target_path),
    ]
    subprocess.run(command, capture_output=True, text=True, check=True, timeout=5 * 60)


def _extract_visual_window_frames(
    *,
    source_path: Path,
    transcript_id: UUID,
    window: TranscriptWindow,
) -> dict:
    sample_timestamps = _window_sample_timestamps(window.start_ms, window.end_ms)
    sample_manifest: list[dict] = []
    for sample_index, timestamp_ms in enumerate(sample_timestamps):
        frame_path = visual_window_sample_path(transcript_id, window.window_index, sample_index)
        _extract_frame_at_timestamp(source_path, frame_path, timestamp_ms)
        sample_manifest.append(
            {
                "sample_index": sample_index,
                "timestamp_ms": timestamp_ms,
                "path": str(frame_path),
            }
        )

    thumbnail_timestamp_ms = window.start_ms + max(0, (window.end_ms - window.start_ms) // 2)
    thumbnail_path = visual_window_thumbnail_path(transcript_id, window.window_index)
    _extract_frame_at_timestamp(source_path, thumbnail_path, thumbnail_timestamp_ms)

    return {
        "sample_frames": sample_manifest,
        "thumbnail": {
            "timestamp_ms": thumbnail_timestamp_ms,
            "path": str(thumbnail_path),
        },
    }


def _index_visual_window_descriptions(
    db,
    *,
    transcript: Transcript,
    video: Video,
    owner_id: UUID,
    transcript_windows: list[TranscriptWindow],
) -> dict:
    transcript_media_dir = visual_window_transcript_dir(transcript.id)
    existing_ready_count, existing_failed_count = _visual_status_counts(db, transcript_id=transcript.id)

    if not transcript_windows:
        return {
            "status": "no_windows",
            "visual_descriptions_ready": existing_ready_count,
            "visual_descriptions_failed": existing_failed_count,
        }

    source_path = _resolve_video_input_path(video)
    if not source_path.exists() or not source_path.is_file():
        return {
            "status": "missing_source",
            "visual_descriptions_ready": existing_ready_count,
            "visual_descriptions_failed": existing_failed_count,
        }

    try:
        vision_provider = get_vision_provider()
    except AIProviderError as exc:
        return {
            "status": "provider_unavailable",
            "error": str(exc),
            "visual_descriptions_ready": existing_ready_count,
            "visual_descriptions_failed": existing_failed_count,
        }

    try:
        local_embedding_provider = get_local_embedding_provider()
    except AIProviderError as exc:
        return {
            "status": "embedding_provider_error",
            "error": str(exc),
            "visual_descriptions_ready": existing_ready_count,
            "visual_descriptions_failed": existing_failed_count,
        }

    db.execute(delete(VisualWindowDescription).where(VisualWindowDescription.transcript_id == transcript.id))
    db.flush()
    shutil.rmtree(transcript_media_dir, ignore_errors=True)

    ready_count = 0
    failed_count = 0

    for window in transcript_windows:
        visual_row = VisualWindowDescription(
            transcript_window_id=window.id,
            transcript_id=transcript.id,
            video_id=video.id,
            start_ms=window.start_ms,
            end_ms=window.end_ms,
            status="pending",
        )
        db.add(visual_row)
        db.flush()

        try:
            manifest = _extract_visual_window_frames(source_path=source_path, transcript_id=transcript.id, window=window)
            manifest["prompt_version"] = settings.visual_description_prompt_version
            visual_row.thumbnail_path = manifest["thumbnail"]["path"]
            visual_row.frame_manifest_json = manifest
            db.add(visual_row)

            prompt = _visual_prompt_for_window(window)
            description_result = vision_provider.describe_images(
                [frame["path"] for frame in manifest["sample_frames"]],
                prompt,
            )
            visual_row.generator_provider = getattr(vision_provider, "provider_name", "openai")
            visual_row.generator_model = description_result.model
            visual_row.description_text = description_result.description_text

            embedded = local_embedding_provider.embed_texts([description_result.description_text])
            visual_row.embedding_provider = getattr(local_embedding_provider, "provider_name", "local")
            visual_row.embedding_model = embedded.model
            visual_row.embedding_vector = embedded.vectors[0]
            visual_row.status = "ready"
            db.add(visual_row)

            _log_model_run(
                db,
                owner_id=owner_id,
                video_id=video.id,
                provider_name=visual_row.generator_provider or "openai",
                operation="vision.describe_window",
                model_name=visual_row.generator_model or "unknown",
                input_tokens=description_result.total_tokens,
                metadata={
                    "transcript_id": str(transcript.id),
                    "transcript_window_id": str(window.id),
                    "window_index": window.window_index,
                    "sample_image_count": len(manifest["sample_frames"]),
                    "prompt_version": settings.visual_description_prompt_version,
                    "prompt_chars": len(prompt),
                },
            )
            _log_model_run(
                db,
                owner_id=owner_id,
                video_id=video.id,
                provider_name=visual_row.embedding_provider or "local",
                operation="embedding.visual_description",
                model_name=visual_row.embedding_model or "unknown",
                metadata={
                    "transcript_id": str(transcript.id),
                    "transcript_window_id": str(window.id),
                    "window_index": window.window_index,
                },
            )
            ready_count += 1
        except Exception as exc:
            logger.exception(
                "Visual window description indexing failed for transcript_id=%s window_index=%s",
                transcript.id,
                window.window_index,
            )
            visual_row.status = "failed"
            db.add(visual_row)
            failed_count += 1

    overall_status = "ready" if failed_count == 0 else ("partial" if ready_count else "failed")

    return {
        "status": overall_status,
        "visual_descriptions_ready": ready_count,
        "visual_descriptions_failed": failed_count,
    }


def export_clip_job(clip_id: str) -> dict:
    with SessionLocal() as db:
        clip = db.get(Clip, UUID(clip_id))
        if clip is None:
            logger.warning("Clip export received unknown clip_id=%s", clip_id)
            return {"status": "missing_clip", "clip_id": clip_id}

        clip.status = ClipStatus.PROCESSING
        db.add(clip)
        db.commit()

        video = db.get(Video, clip.video_id)
        if video is None:
            clip.status = ClipStatus.FAILED
            db.add(clip)
            db.commit()
            return {"status": "missing_video", "clip_id": clip_id}

        start_seconds = max(0.0, clip.start_ms / 1000.0)
        duration_seconds = max(0.1, (clip.end_ms - clip.start_ms) / 1000.0)

        normalized_input = video_playback_path(video)
        source_path = (
            normalized_input
            if normalized_input is not None and normalized_input.exists()
            else Path(video.source_path)
        )
        if not source_path.exists() or not source_path.is_file():
            clip.status = ClipStatus.FAILED
            db.add(clip)
            db.commit()
            return {"status": "missing_source", "clip_id": clip_id}

        out_dir = clip_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = clip_output_path(clip_id)
        tmp_path = clip_temp_output_path(clip_id)
        transcript_path = clip_transcript_path(clip_id)
        poster_path = clip_poster_path(clip_id)

        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            str(tmp_path),
        ]

        try:
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=30 * 60)

            if out_path.exists():
                out_path.unlink()
            tmp_path.replace(out_path)

            poster_command = [
                "ffmpeg",
                "-y",
                "-ss",
                "0.050",
                "-i",
                str(out_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(poster_path),
            ]
            subprocess.run(poster_command, capture_output=True, text=True, check=True, timeout=5 * 60)

            latest_transcript = db.execute(
                select(Transcript).where(Transcript.video_id == video.id).order_by(Transcript.updated_at.desc())
            ).scalars().first()

            excerpt_lines: list[str] = []
            if latest_transcript is not None:
                clip_segments = db.execute(
                    select(Segment)
                    .where(
                        Segment.transcript_id == latest_transcript.id,
                        Segment.end_ms >= clip.start_ms,
                        Segment.start_ms <= clip.end_ms,
                    )
                    .order_by(Segment.start_ms.asc())
                ).scalars().all()

                for seg in clip_segments:
                    excerpt_lines.append(f"[{_format_ts(seg.start_ms)}] {seg.text}")

            if not excerpt_lines:
                excerpt_lines = ["No transcript segments overlapped the selected timestamp range."]

            transcript_payload = [
                f"Clip ID: {clip.id}",
                f"Video: {video.title}",
                f"Range: {_format_ts(clip.start_ms)} - {_format_ts(clip.end_ms)}",
                "",
                "Timestamped Transcript",
                "---------------------",
                *excerpt_lines,
            ]
            transcript_path.write_text("\n".join(transcript_payload), encoding="utf-8")

            clip.output_mp4_path = str(out_path)
            clip.metadata_json_path = str(transcript_path)
            clip.transcript_excerpt = "\n".join(excerpt_lines)
            clip.citation_text = (
                f"{video.title}. Clip {_format_ts(clip.start_ms)}-{_format_ts(clip.end_ms)}. "
                f"Semantic clip ID: {clip.id}."
            )
            clip.status = ClipStatus.COMPLETE
            db.add(clip)
            db.commit()
            return {
                "status": "completed",
                "clip_id": clip_id,
                "output_mp4_path": str(out_path),
                "poster_path": str(poster_path),
                "transcript_path": str(transcript_path),
            }
        except Exception as exc:
            logger.exception("Clip export failed for clip_id=%s", clip_id)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if poster_path.exists():
                poster_path.unlink(missing_ok=True)
            clip.status = ClipStatus.FAILED
            db.add(clip)
            db.commit()
            return {"status": "failed", "clip_id": clip_id, "error": str(exc)}


def _transcode_video_with_lock(db, video: Video, video_id: str) -> dict:
    started_at = datetime.now(timezone.utc)
    output_dir = transcode_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = video_playback_path(video)
    prior_delivery_state = _capture_video_delivery_state(video)
    preserve_ready_playback = (
        video.status == VideoStatus.READY
        and output_path is not None
        and output_path.is_file()
    )
    _set_video_transcode_state(
        db,
        video,
        status=None if preserve_ready_playback else VideoStatus.TRANSCODING,
        progress_pct=0,
        stage="preparing",
        started_at=started_at,
    )

    source_path = Path(video.source_path)
    if not source_path.exists() or not source_path.is_file():
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="failed",
        )
        return {"status": "missing_source", "video_id": video_id}

    temp_output_path = transcode_temp_output_path(video_id, uuid4().hex)

    try:
        require_public_playback_ffmpeg_runtime()
        source_probe = probe_media(source_path)
        command = public_playback_transcode_command(
            source_path,
            temp_output_path,
            probe=source_probe,
        )
        duration_ms = max(1, int(round(source_probe.duration_seconds * 1000)))
        committed_progress_pct = 0

        def handle_progress_line(line: str) -> None:
            nonlocal committed_progress_pct
            key, _, value = line.partition("=")
            if key in {"out_time_ms", "out_time_us"}:
                try:
                    encoded_us = int(value)
                except ValueError:
                    return
                raw_pct = max(1, min(99, int((encoded_us / 1000) / duration_ms * 100)))
                # At most twenty progress commits per encode. This keeps hundreds of
                # concurrent library jobs from turning progress reporting into the
                # dominant database workload.
                next_pct = min(95, (raw_pct // 5) * 5)
                if next_pct >= committed_progress_pct + 5:
                    committed_progress_pct = next_pct
                    _set_video_transcode_state(
                        db,
                        video,
                        status=None if preserve_ready_playback else VideoStatus.TRANSCODING,
                        progress_pct=next_pct,
                        stage="transcoding",
                    )
            elif key == "progress" and value == "end" and committed_progress_pct < 99:
                committed_progress_pct = 99
                _set_video_transcode_state(
                    db,
                    video,
                    status=None if preserve_ready_playback else VideoStatus.TRANSCODING,
                    progress_pct=99,
                    stage="validating",
                )

        process_result = run_transcode_process(command, on_progress_line=handle_progress_line)
        diagnostic_tail = _redact_transcode_diagnostics(
            process_result.stderr_tail,
            source_path,
            temp_output_path,
            *(path for path in (output_path,) if path is not None),
        )
        if diagnostic_tail.strip():
            logger.warning(
                "FFmpeg transcode diagnostics for video_id=%s: %s",
                video_id,
                diagnostic_tail.strip(),
            )
        if process_result.return_code != 0:
            raise subprocess.CalledProcessError(
                process_result.return_code,
                command,
                output="\n".join(process_result.progress_lines),
                stderr=diagnostic_tail,
            )
        if "level limit" in diagnostic_tail.lower():
            raise MediaValidationError("encoder_reported_h264_level_violation")

        artifact = validate_public_playback_artifact(
            temp_output_path,
            source_probe=source_probe,
        )
        _publish_validated_playback(
            db,
            video,
            artifact=artifact,
            temp_output_path=temp_output_path,
            publication_token=uuid4().hex,
        )
        logger.info(
            "FFmpeg transcode completed for video_id=%s recipe=%s sha256=%s bytes=%s",
            video_id,
            artifact.recipe_revision,
            artifact.sha256,
            artifact.size_bytes,
        )
        return {
            "status": "completed",
            "video_id": video_id,
            "playback_revision": artifact.recipe_revision,
            "playback_sha256": artifact.sha256,
            "playback_size_bytes": artifact.size_bytes,
            "color_policy": artifact.color_policy,
        }
    except MediaReviewRequired as exc:
        logger.warning(
            "Transcode requires media review for video_id=%s reason=%s",
            video_id,
            exc.reason_code,
        )
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="review_required",
        )
        return {
            "status": "review_required",
            "video_id": video_id,
            "reason": exc.reason_code,
        }
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg transcode timed out for video_id=%s", video_id)
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="timed_out",
        )
        return {"status": "timed_out", "video_id": video_id}
    except subprocess.CalledProcessError as exc:
        diagnostic_tail = _redact_transcode_diagnostics(
            exc.stderr or "",
            source_path,
            temp_output_path,
            *(path for path in (output_path,) if path is not None),
        )
        logger.error(
            "FFmpeg transcode failed for video_id=%s return_code=%s diagnostics=%s",
            video_id,
            exc.returncode,
            diagnostic_tail,
        )
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="failed",
        )
        return {
            "status": "ffmpeg_failed",
            "video_id": video_id,
            "stderr": diagnostic_tail,
        }
    except (MediaProbeError, MediaValidationError) as exc:
        logger.error(
            "Transcode validation failed for video_id=%s reason=%s",
            video_id,
            str(exc),
        )
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="validation_failed",
        )
        return {
            "status": "validation_failed",
            "video_id": video_id,
            "reason": str(exc),
        }
    except Exception:
        logger.exception("Unexpected transcode failure for video_id=%s", video_id)
        _finish_failed_transcode_attempt(
            db,
            video,
            prior_delivery_state=prior_delivery_state,
            preserve_ready_playback=preserve_ready_playback,
            failed_stage="failed",
        )
        return {"status": "failed", "video_id": video_id, "reason": "unexpected_error"}
    finally:
        if temp_output_path.exists():
            try:
                temp_output_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Unable to remove a transcode temporary file for video_id=%s",
                    video_id,
                )


def transcode_video_job(video_id: str) -> dict:
    with SessionLocal() as db:
        video = db.get(Video, UUID(video_id))
        if video is None:
            logger.warning("Transcode job received unknown video_id=%s", video_id)
            return {"status": "missing_video", "video_id": video_id}

        output_dir = transcode_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_transcode_lock(video_id) as lock_acquired:
            if not lock_acquired:
                logger.info("Skipping duplicate active transcode for video_id=%s", video_id)
                return {"status": "already_running", "video_id": video_id}
            return _transcode_video_with_lock(db, video, video_id)


def transcribe_video_job(video_id: str, language: str = "en") -> dict:
    with SessionLocal() as db:
        video = db.get(Video, UUID(video_id))
        if video is None:
            logger.warning("Transcription job received unknown video_id=%s", video_id)
            return {"status": "missing_video", "video_id": video_id}

        owner_id = _owner_id_for_video(db, video.project_id)
        if owner_id is None:
            logger.error("Video has no owning project: video_id=%s", video_id)
            return {"status": "missing_project", "video_id": video_id}

        has_transcript = db.execute(select(Transcript.id).where(Transcript.video_id == video.id).limit(1)).scalar_one_or_none()
        if has_transcript:
            logger.info("Skipping auto-transcription; transcript already exists for video_id=%s", video_id)
            return {"status": "skipped_existing_transcript", "video_id": video_id}

        try:
            provider = get_openai_provider()
            result = provider.transcribe_audio(video.source_path, language=language)
        except AIProviderError as exc:
            logger.exception("Auto-transcription failed for video_id=%s", video_id)
            return {"status": "provider_error", "video_id": video_id, "error": str(exc)}

        parsed_segments = _segments_from_transcription(result.text, result.segments)

        transcript = Transcript(
            video_id=video.id,
            source=TranscriptSource.AUTO,
            language=language,
            format=TranscriptFormat.PLAIN,
            raw_text=result.text,
        )
        db.add(transcript)
        db.flush()

        for segment in parsed_segments:
            db.add(
                Segment(
                    transcript_id=transcript.id,
                    position=segment.position,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                )
            )

        _log_model_run(
            db,
            owner_id=owner_id,
            video_id=video.id,
            provider_name="openai",
            operation="transcription.auto",
            model_name=result.model,
            metadata={
                "language": language,
                "segments_created": len(parsed_segments),
            },
        )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.warning("Transcription commit conflicted; transcript likely created concurrently for video_id=%s", video_id)
            return {"status": "conflict_existing_transcript", "video_id": video_id}

    logger.info("Auto-transcription completed for video_id=%s", video_id)
    return {
        "status": "completed",
        "video_id": video_id,
        "transcript_segments": len(parsed_segments),
        "language": language,
    }


def index_transcript_job(transcript_id: str) -> dict:
    with SessionLocal() as db:
        transcript = db.get(Transcript, UUID(transcript_id))
        if transcript is None:
            logger.warning("Index job received unknown transcript_id=%s", transcript_id)
            return {"status": "missing_transcript", "transcript_id": transcript_id}

        video = db.get(Video, transcript.video_id)
        if video is None:
            return {"status": "missing_video", "transcript_id": transcript_id}

        owner_id = _owner_id_for_video(db, video.project_id)
        if owner_id is None:
            return {"status": "missing_project", "transcript_id": transcript_id}

        segments = db.execute(
            select(Segment).where(Segment.transcript_id == transcript.id).order_by(Segment.position.asc())
        ).scalars().all()

        if not segments:
            try:
                parsed_segments = parse_transcript(transcript.raw_text, transcript.format)
            except ValueError as exc:
                logger.error("Failed to parse transcript %s for indexing: %s", transcript_id, exc)
                return {"status": "parse_error", "transcript_id": transcript_id, "error": str(exc)}
            for segment in parsed_segments:
                db.add(
                    Segment(
                        transcript_id=transcript.id,
                        position=segment.position,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=segment.text,
                    )
                )
            db.flush()
            segments = db.execute(
                select(Segment).where(Segment.transcript_id == transcript.id).order_by(Segment.position.asc())
            ).scalars().all()

        if not segments:
            return {"status": "no_segments", "transcript_id": transcript_id}

        transcript_windows = _sync_transcript_windows(db, transcript_id=transcript.id, segments=segments)

        try:
            provider = get_embedding_provider()
        except AIProviderError as exc:
            logger.exception("Indexing provider unavailable for transcript_id=%s", transcript_id)
            return {"status": "provider_error", "transcript_id": transcript_id, "error": str(exc)}

        total_tokens = 0
        model_name = settings.openai_embedding_model
        for idx in range(0, len(segments), settings.embedding_batch_size):
            batch = segments[idx : idx + settings.embedding_batch_size]
            texts = [segment.text for segment in batch]
            try:
                result = provider.embed_texts(texts)
            except AIProviderError as exc:
                logger.exception("Embedding batch failed for transcript_id=%s", transcript_id)
                db.rollback()
                return {"status": "provider_error", "transcript_id": transcript_id, "error": str(exc)}

            model_name = result.model
            if result.total_tokens:
                total_tokens += result.total_tokens

            if len(result.vectors) != len(batch):
                db.rollback()
                return {
                    "status": "provider_error",
                    "transcript_id": transcript_id,
                    "error": "Embedding vector count mismatch",
                }

            for segment, vector in zip(batch, result.vectors):
                segment.embedding_vector = vector
                db.add(segment)

        for idx in range(0, len(transcript_windows), settings.embedding_batch_size):
            batch = transcript_windows[idx : idx + settings.embedding_batch_size]
            texts = [window.text for window in batch]
            try:
                result = provider.embed_texts(texts)
            except AIProviderError as exc:
                logger.exception("Transcript-window embedding batch failed for transcript_id=%s", transcript_id)
                db.rollback()
                return {"status": "provider_error", "transcript_id": transcript_id, "error": str(exc)}

            model_name = result.model
            if result.total_tokens:
                total_tokens += result.total_tokens

            if len(result.vectors) != len(batch):
                db.rollback()
                return {
                    "status": "provider_error",
                    "transcript_id": transcript_id,
                    "error": "Transcript-window embedding vector count mismatch",
                }

            for window, vector in zip(batch, result.vectors):
                window.embedding_vector = vector
                db.add(window)

        _log_model_run(
            db,
            owner_id=owner_id,
            video_id=video.id,
            provider_name=getattr(provider, "provider_name", "openai"),
            operation="embedding.index",
            model_name=model_name,
            input_tokens=total_tokens or None,
            metadata={
                "transcript_id": transcript_id,
                "segments_indexed": len(segments),
                "transcript_windows_indexed": len(transcript_windows),
            },
        )

        visual_index_result = _index_visual_window_descriptions(
            db,
            transcript=transcript,
            video=video,
            owner_id=owner_id,
            transcript_windows=transcript_windows,
        )

        db.commit()

    logger.info("Transcript indexing completed for transcript_id=%s", transcript_id)
    return {
        "status": "completed",
        "transcript_id": transcript_id,
        "segments_indexed": len(segments),
        "transcript_windows_indexed": len(transcript_windows),
        "visual_description_status": visual_index_result["status"],
        "visual_descriptions_ready": visual_index_result["visual_descriptions_ready"],
        "visual_descriptions_failed": visual_index_result["visual_descriptions_failed"],
    }


def ingest_title_card_ocr_job(
    video_id: str,
    fixture_path: str | None = None,
    replace_existing: bool = False,
    run_live: bool = False,
) -> dict:
    with SessionLocal() as db:
        video = db.get(Video, UUID(video_id))
        if video is None:
            logger.warning("Title-card OCR job received unknown video_id=%s", video_id)
            return {"status": "missing_video", "video_id": video_id}

        owner_id = _owner_id_for_video(db, video.project_id)
        if owner_id is None:
            logger.error("Title-card OCR job missing project owner for video_id=%s", video_id)
            return {"status": "missing_project", "video_id": video_id}

        if fixture_path:
            try:
                batch = load_title_card_import_batch(fixture_path)
            except (FileNotFoundError, ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
                logger.exception("Title-card OCR fixture import failed for video_id=%s", video_id)
                return {"status": "fixture_error", "video_id": video_id, "error": str(exc)}

            model_run = _log_model_run(
                db,
                owner_id=owner_id,
                video_id=video.id,
                provider_name=batch.model_provider or "fixture",
                operation="title_card_ocr.import",
                model_name=batch.model_name or "fixture-import",
                metadata={
                    "fixture_path": fixture_path,
                    "detail": batch.detail or settings.title_card_ocr_detail,
                    "prompt_version": batch.prompt_version or settings.title_card_prompt_version,
                    "source_kind": batch.source_kind or "fixture_import",
                    "source_version": batch.source_version or "structured-json-v1",
                    "replace_existing": bool(replace_existing),
                    "observations_requested": len(batch.observations),
                },
            )
            summary = import_title_card_observation_batch(
                db,
                video=video,
                batch=batch,
                model_run_id=model_run.id,
                replace_existing=replace_existing,
            )
            db.commit()
            return {
                "status": "completed",
                "mode": "fixture_import",
                "video_id": video_id,
                "model_run_id": str(model_run.id),
                **summary,
            }

        if not run_live:
            return {
                "status": "live_ocr_not_requested",
                "video_id": video_id,
            }

        source_path = _resolve_video_input_path(video)
        if not source_path.exists() or not source_path.is_file():
            return {"status": "missing_source", "video_id": video_id, "source_path": str(source_path)}

        try:
            provider = get_vision_provider()
        except AIProviderError as exc:
            logger.exception("Title-card OCR provider unavailable for video_id=%s", video_id)
            return {"status": "provider_error", "video_id": video_id, "error": str(exc)}

        sample_timestamps = title_card_sample_timestamps(video.duration_ms)
        prompt = build_title_card_ocr_prompt()
        detail = settings.title_card_ocr_detail
        prompt_version = settings.title_card_prompt_version
        provider_name = getattr(provider, "provider_name", "openai")
        source_kind = "vision_ocr"
        source_version = f"cadence-{settings.title_card_sample_cadence_seconds}s-detail-{detail}"
        observation_drafts: list[TitleCardObservationDraft] = []
        prompt_tokens_total = 0
        output_tokens_total = 0
        total_tokens = 0
        observed_models: set[str] = set()
        temp_dir = Path(tempfile.mkdtemp(prefix=f"title-card-ocr-{video.id}-"))

        try:
            for sample_index, timestamp_ms in enumerate(sample_timestamps):
                frame_path = temp_dir / f"sample-{sample_index:04d}.jpg"
                _extract_frame_at_timestamp(source_path, frame_path, timestamp_ms)
                result = provider.extract_title_card(
                    str(frame_path),
                    prompt,
                    detail=detail,
                    max_tokens=settings.title_card_ocr_max_tokens,
                )
                observation_drafts.append(
                    TitleCardObservationDraft(
                        timestamp_ms=timestamp_ms,
                        payload=result.payload,
                        status="ready",
                        prompt_version=prompt_version,
                        model_provider=provider_name,
                        model_name=result.model,
                        detail=detail,
                        source_kind=source_kind,
                        source_version=source_version,
                    )
                )
                observed_models.add(result.model)
                if result.prompt_tokens:
                    prompt_tokens_total += result.prompt_tokens
                if result.output_tokens:
                    output_tokens_total += result.output_tokens
                if result.total_tokens:
                    total_tokens += result.total_tokens
        except subprocess.CalledProcessError as exc:
            logger.exception("Title-card OCR frame extraction failed for video_id=%s", video_id)
            db.rollback()
            return {
                "status": "ffmpeg_failed",
                "video_id": video_id,
                "stderr": (exc.stderr or "")[-4000:],
            }
        except AIProviderError as exc:
            logger.exception("Title-card OCR extraction failed for video_id=%s", video_id)
            db.rollback()
            return {"status": "provider_error", "video_id": video_id, "error": str(exc)}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        batch = TitleCardImportBatch(
            prompt_version=prompt_version,
            model_provider=provider_name,
            model_name=next(iter(observed_models)) if len(observed_models) == 1 else settings.openai_vision_model,
            detail=detail,
            source_kind=source_kind,
            source_version=source_version,
            observations=observation_drafts,
        )
        model_run = _log_model_run(
            db,
            owner_id=owner_id,
            video_id=video.id,
            provider_name=provider_name,
            operation="title_card_ocr.extract",
            model_name=batch.model_name or settings.openai_vision_model,
            input_tokens=prompt_tokens_total or total_tokens or None,
            output_tokens=output_tokens_total or None,
            metadata={
                "detail": detail,
                "prompt_version": prompt_version,
                "source_kind": source_kind,
                "source_version": source_version,
                "cadence_seconds": settings.title_card_sample_cadence_seconds,
                "sampled_frames": len(sample_timestamps),
                "replace_existing": bool(replace_existing),
                "video_duration_ms": video.duration_ms,
                "total_tokens": total_tokens or None,
            },
        )
        summary = import_title_card_observation_batch(
            db,
            video=video,
            batch=batch,
            model_run_id=model_run.id,
            replace_existing=replace_existing,
        )
        db.commit()
        return {
            "status": "completed",
            "mode": "live_ocr",
            "video_id": video_id,
            "sampled_frames": len(sample_timestamps),
            "model_run_id": str(model_run.id),
            **summary,
        }


def build_corpus_phrases_job(project_id: str) -> dict:
    from app.services.corpus_phrase_builder import build_corpus_phrases_for_project

    try:
        counts = build_corpus_phrases_for_project(UUID(project_id))
    except Exception as exc:
        logger.exception("Corpus phrase build failed for project_id=%s", project_id)
        return {"status": "failed", "project_id": project_id, "error": str(exc)}

    return {"status": "completed", "project_id": project_id, "counts": counts}


def _owner_id_for_video(db, project_id: UUID):
    return db.execute(select(Project.owner_id).where(Project.id == project_id)).scalar_one_or_none()


def _segments_from_transcription(text: str, result_segments) -> list[ParsedSegment]:
    if result_segments:
        parsed: list[ParsedSegment] = []
        for idx, segment in enumerate(result_segments):
            if not segment.text.strip():
                continue
            parsed.append(
                ParsedSegment(
                    position=idx,
                    start_ms=max(segment.start_ms, 0),
                    end_ms=max(segment.end_ms, max(segment.start_ms, 0)),
                    text=segment.text.strip(),
                )
            )
        if parsed:
            return parsed

    return parse_transcript(text, TranscriptFormat.PLAIN)


def _active_provider_id(db, owner_id: UUID, provider_name: str) -> UUID | None:
    provider = db.execute(
        select(ModelProvider)
        .where(
            ModelProvider.user_id == owner_id,
            ModelProvider.provider_name == provider_name,
            ModelProvider.is_active.is_(True),
        )
        .order_by(ModelProvider.updated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return provider.id if provider else None


def _log_model_run(
    db,
    owner_id: UUID,
    video_id: UUID | None,
    provider_name: str,
    operation: str,
    model_name: str,
    metadata: dict,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> ModelRun:
    model_run = ModelRun(
        provider_id=_active_provider_id(db, owner_id, provider_name),
        user_id=owner_id,
        video_id=video_id,
        operation=operation,
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal("0"),
        metadata_json=metadata,
    )
    db.add(model_run)
    db.flush()
    return model_run
