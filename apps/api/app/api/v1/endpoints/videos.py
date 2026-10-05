import hashlib
import subprocess
from pathlib import Path
from uuid import uuid4
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_stream
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import Object, Project, User, Video, VideoStatus
from app.schemas.video import MediaFileResponse, VideoCreate, VideoCreateResponse, VideoResponse, VideoUpdate
from app.services.audit_events import append_metadata_change_event, append_publication_state_event, build_user_actor_payload
from app.services.media_transcode import transcode_output_path, video_playback_path
from app.services.job_queue import enqueue_transcode, enqueue_transcription
from app.services.upload_validation import UploadTooLargeError, UploadValidationError, persist_bounded_upload

router = APIRouter(prefix="/videos", tags=["videos"])


def _owned_project(db: Session, project_id: UUID, owner_id: UUID) -> Project | None:
    return db.execute(select(Project).where(Project.id == project_id, Project.owner_id == owner_id)).scalar_one_or_none()


def _owned_object(db: Session, object_id: UUID, owner_id: UUID) -> Object | None:
    return db.execute(
        select(Object).join(Project, Project.id == Object.project_id).where(Object.id == object_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


@router.post("", response_model=VideoCreateResponse, status_code=status.HTTP_201_CREATED)
def create_video(
    payload: VideoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VideoCreateResponse:
    if _owned_project(db, payload.project_id, current_user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if payload.object_id and _owned_object(db, payload.object_id, current_user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    source_path = _resolve_media_file(payload.source_path)
    video_payload = payload.model_dump(
        exclude={"enqueue_transcode", "auto_transcribe_if_missing", "transcription_language"}
    )
    video_payload["source_path"] = str(source_path)
    video_payload["original_filename"] = payload.original_filename or source_path.name
    video_payload["sha256_checksum"] = payload.sha256_checksum or _sha256_file(source_path)
    video_payload["duration_ms"] = payload.duration_ms if payload.duration_ms is not None else _duration_ms(source_path)

    video = Video(**video_payload)
    if payload.enqueue_transcode:
        video.status = VideoStatus.TRANSCODING
        video.transcode_progress_pct = 0
        video.transcode_stage = "queued"
    db.add(video)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stable video ID already exists. Use a new Stable video ID.",
        ) from exc

    db.refresh(video)

    transcode_job_id = None
    transcription_job_id = None
    if payload.enqueue_transcode:
        transcode_job_id = enqueue_transcode(video.id)
        if transcode_job_id is None:
            video.status = VideoStatus.FAILED
            video.transcode_stage = "failed"
            db.add(video)
            db.commit()
            db.refresh(video)
    if payload.auto_transcribe_if_missing:
        transcription_job_id = enqueue_transcription(video.id, language=payload.transcription_language)

    return VideoCreateResponse(
        video=video,
        transcode_job_id=transcode_job_id,
        transcription_job_id=transcription_job_id,
    )


@router.get("/media-files", response_model=list[MediaFileResponse])
def list_media_files(current_user: User = Depends(get_current_user)) -> list[MediaFileResponse]:
    # Keep auth requirement; this is operator-facing inventory.
    _ = current_user
    media_root = Path(settings.media_root).resolve()
    if not media_root.exists():
        return []

    extensions = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".webm"}
    files: list[MediaFileResponse] = []
    for path in media_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        stat = path.stat()
        files.append(
            MediaFileResponse(
                path=str(path.resolve()),
                filename=path.name,
                size_bytes=stat.st_size,
            )
        )

    files.sort(key=lambda item: item.filename.lower())
    return files


@router.post("/media-files", response_model=MediaFileResponse, status_code=status.HTTP_201_CREATED)
def upload_media_file(
    media_file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> MediaFileResponse:
    _ = current_user
    filename = Path(media_file.filename or "").name
    if not filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A media file is required")

    extension = Path(filename).suffix.lower()
    allowed = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".webm"}
    if extension not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported media file type")

    media_root = Path(settings.media_root).resolve()
    media_root.mkdir(parents=True, exist_ok=True)

    destination = media_root / filename
    for attempt in range(10):
        if attempt:
            destination = media_root / f"{Path(filename).stem}_{uuid4().hex[:8]}{extension}"
        try:
            persist_bounded_upload(
                media_file.file,
                destination,
                max_bytes=settings.media_upload_max_bytes,
            )
            break
        except FileExistsError:
            continue
        except UploadTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Media upload exceeds the configured size limit.",
            ) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Media upload is empty.") from exc
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A unique media destination could not be allocated.",
        )

    stat = destination.stat()
    return MediaFileResponse(path=str(destination), filename=destination.name, size_bytes=stat.st_size)


@router.get("", response_model=list[VideoResponse])
def list_videos(
    project_id: UUID | None = Query(default=None),
    object_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Video]:
    query = select(Video).join(Project, Project.id == Video.project_id).where(Project.owner_id == current_user.id)
    if project_id is not None:
        query = query.where(Video.project_id == project_id)
    if object_id is not None:
        query = query.where(Video.object_id == object_id)

    return db.execute(query.order_by(Video.created_at.desc())).scalars().all()


@router.get("/{video_id}", response_model=VideoResponse)
def get_video(video_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> Video:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return video


@router.get("/{video_id}/stream")
def stream_video(video_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user_stream)) -> FileResponse:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    if video.status != VideoStatus.READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video is still processing. Playback is enabled when status is READY.",
        )

    playback_path = video_playback_path(video)
    if playback_path is None or not playback_path.exists() or not playback_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Normalized playback asset is unavailable for this video.",
        )

    return FileResponse(path=playback_path, filename=f"{Path(video.original_filename).stem}.normalized.mp4")


@router.post("/transcode/backfill")
def backfill_transcodes(
    project_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    query = select(Video).join(Project, Project.id == Video.project_id).where(Project.owner_id == current_user.id)
    if project_id is not None:
        query = query.where(Video.project_id == project_id)

    videos = db.execute(query).scalars().all()
    queued = 0
    skipped_ready = 0
    failed_queue = 0

    for video in videos:
        if video.status == VideoStatus.READY and transcode_output_path(str(video.id)).exists():
            skipped_ready += 1
            continue

        video.status = VideoStatus.TRANSCODING
        video.transcode_progress_pct = 0
        video.transcode_stage = "queued"
        db.add(video)
        db.flush()

        job_id = enqueue_transcode(video.id)
        if job_id is None:
            video.status = VideoStatus.FAILED
            video.transcode_stage = "failed"
            db.add(video)
            failed_queue += 1
        else:
            queued += 1

    db.commit()
    return {
        "queued": queued,
        "skipped_ready": skipped_ready,
        "failed_queue": failed_queue,
        "total_videos": len(videos),
    }


@router.post("/{video_id}/transcode")
def enqueue_video_transcode(
    video_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    video.status = VideoStatus.TRANSCODING
    video.transcode_progress_pct = 0
    video.transcode_stage = "queued"
    db.add(video)
    db.commit()

    job_id = enqueue_transcode(video.id)
    if job_id is None:
        video.status = VideoStatus.FAILED
        video.transcode_stage = "failed"
        db.add(video)
        db.commit()
        return {"queued": False, "video_id": str(video.id), "reason": "queue_unavailable"}

    return {"queued": True, "video_id": str(video.id), "job_id": job_id}


@router.patch("/{video_id}", response_model=VideoResponse)
def update_video(
    video_id: UUID,
    payload: VideoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Video:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    updates = payload.model_dump(exclude_unset=True)
    object_id = updates.get("object_id")
    if object_id and _owned_object(db, object_id, current_user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    previous_is_published = bool(video.is_published)
    previous_values = {
        "object_id": video.object_id,
        "title": video.title,
        "original_filename": video.original_filename,
        "source_path": video.source_path,
        "duration_ms": video.duration_ms,
        "status": video.status,
    }

    for key, value in updates.items():
        setattr(video, key, value)

    object_row = db.get(Object, video.object_id) if video.object_id else None
    actor_json = build_user_actor_payload(current_user, trigger="api.videos.update")
    append_publication_state_event(
        db,
        subject_type="video",
        subject_id=video.id,
        actor_json=actor_json,
        root_public_id=object_row.website_object_id if object_row is not None else None,
        previous_value=previous_is_published,
        next_value=bool(video.is_published),
    )
    append_metadata_change_event(
        db,
        subject_type="video",
        subject_id=video.id,
        actor_json=actor_json,
        root_public_id=object_row.website_object_id if object_row is not None else None,
        changes={
            key: {"before": previous_values[key], "after": getattr(video, key)}
            for key in previous_values
            if key in updates and previous_values[key] != getattr(video, key)
        },
    )

    db.add(video)
    db.commit()
    db.refresh(video)
    return video


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(video_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> None:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    db.delete(video)
    db.commit()


def _resolve_media_file(path_value: str) -> Path:
    path = Path(path_value).resolve()
    media_root = Path(settings.media_root).resolve()
    try:
        path.relative_to(media_root)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source path must be inside media root") from exc

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source file does not exist")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_ms(path: Path) -> int | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20)
        duration = float(result.stdout.strip())
        return max(0, int(duration * 1000))
    except Exception:
        return None
