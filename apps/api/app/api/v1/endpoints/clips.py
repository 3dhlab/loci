from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_stream
from app.db.session import get_db
from app.models.entities import Clip, ClipStatus, Project, Transcript, User, Video
from app.schemas.clip import ClipCreateRequest, ClipCreateResponse, ClipListResponse, ClipResponse
from app.services.job_queue import enqueue_clip_export
from app.services.media_clips import clip_poster_path_for_video

router = APIRouter(prefix="/clips", tags=["clips"])


def _owned_video(db: Session, video_id: UUID, owner_id: UUID) -> Video | None:
    return db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


def _owned_clip(db: Session, clip_id: UUID, owner_id: UUID) -> Clip | None:
    return db.execute(
        select(Clip)
        .join(Project, Project.id == Clip.project_id)
        .where(Clip.id == clip_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


@router.post("", response_model=ClipCreateResponse, status_code=status.HTTP_201_CREATED)
def create_clip(
    payload: ClipCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipCreateResponse:
    video = _owned_video(db, payload.video_id, current_user.id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    if payload.end_ms <= payload.start_ms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_ms must be greater than start_ms")

    clip = Clip(
        video_id=video.id,
        project_id=video.project_id,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        status=ClipStatus.QUEUED,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)

    export_job_id = enqueue_clip_export(clip.id)
    if export_job_id is None:
        clip.status = ClipStatus.FAILED
        db.add(clip)
        db.commit()
        db.refresh(clip)

    return ClipCreateResponse(clip=clip, export_job_id=export_job_id)


@router.get("", response_model=ClipListResponse)
def list_clips(
    project_id: UUID | None = Query(default=None),
    video_id: UUID | None = Query(default=None),
    status_filter: ClipStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipListResponse:
    filters = [Project.owner_id == current_user.id]
    if project_id is not None:
        filters.append(Clip.project_id == project_id)
    if video_id is not None:
        filters.append(Clip.video_id == video_id)
    if status_filter is not None:
        filters.append(Clip.status == status_filter)

    clips = db.execute(
        select(Clip)
        .join(Project, Project.id == Clip.project_id)
        .where(and_(*filters))
        .order_by(Clip.created_at.desc())
        .limit(120)
    ).scalars().all()

    return ClipListResponse(clips=clips)


@router.get("/{clip_id}", response_model=ClipResponse)
def get_clip(
    clip_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipResponse:
    clip = _owned_clip(db, clip_id, current_user.id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return clip


@router.get("/{clip_id}/download")
def download_clip(
    clip_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_stream),
) -> FileResponse:
    clip = _owned_clip(db, clip_id, current_user.id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    if clip.status != ClipStatus.COMPLETE or not clip.output_mp4_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clip is not ready for download")

    clip_path = Path(clip.output_mp4_path)
    if not clip_path.exists() or not clip_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip media file is missing")

    return FileResponse(path=clip_path, filename=f"clip-{clip.id}.mp4", media_type="video/mp4")


@router.get("/{clip_id}/transcript")
def download_clip_transcript(
    clip_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_stream),
) -> FileResponse:
    clip = _owned_clip(db, clip_id, current_user.id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    if clip.status != ClipStatus.COMPLETE or not clip.metadata_json_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clip transcript is not ready")

    transcript_path = Path(clip.metadata_json_path)
    if not transcript_path.exists() or not transcript_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip transcript file is missing")

    return FileResponse(path=transcript_path, filename=f"clip-{clip.id}.transcript.txt", media_type="text/plain")


@router.delete("/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_clip(
    clip_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    clip = _owned_clip(db, clip_id, current_user.id)
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    clip_path = Path(clip.output_mp4_path) if clip.output_mp4_path else None
    transcript_path = Path(clip.metadata_json_path) if clip.metadata_json_path else None
    poster_path = clip_poster_path_for_video(clip_path) if clip_path is not None else None

    db.delete(clip)
    db.commit()

    if clip_path and clip_path.exists():
        clip_path.unlink(missing_ok=True)
    if transcript_path and transcript_path.exists():
        transcript_path.unlink(missing_ok=True)
    if poster_path and poster_path.exists():
        poster_path.unlink(missing_ok=True)
