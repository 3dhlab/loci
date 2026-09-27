from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import Clip, ClipStatus, Object, Video, VideoStatus
from app.services.media_clips import clip_poster_path_for_video

router = APIRouter(prefix="/clips")


def _public_clip_or_404(db: Session, website_clip_id: str) -> Clip:
    normalized = website_clip_id.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published clip not found")

    clip = db.execute(
        select(Clip)
        .join(Video, Video.id == Clip.video_id)
        .join(Object, Object.id == Video.object_id)
        .where(
            Clip.website_clip_id == normalized,
            Clip.status == ClipStatus.COMPLETE,
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Object.is_published.is_(True),
            Object.is_embed_ready.is_(True),
        )
    ).scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published clip not found")
    return clip


@router.get("/{website_clip_id}/stream")
def stream_public_clip(website_clip_id: str, db: Session = Depends(get_db)) -> FileResponse:
    clip = _public_clip_or_404(db, website_clip_id)
    clip_path = Path(clip.output_mp4_path or "")
    if not clip_path.exists() or not clip_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published clip file is missing")

    return FileResponse(
        path=clip_path,
        filename=f"{website_clip_id}.mp4",
        content_disposition_type="inline",
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{website_clip_id}/poster")
def stream_public_clip_poster(website_clip_id: str, db: Session = Depends(get_db)) -> FileResponse:
    clip = _public_clip_or_404(db, website_clip_id)
    poster_path = clip_poster_path_for_video(clip.output_mp4_path or "")
    if not poster_path.exists() or not poster_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published clip poster is missing")

    return FileResponse(
        path=poster_path,
        filename=f"{website_clip_id}.jpg",
        content_disposition_type="inline",
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )