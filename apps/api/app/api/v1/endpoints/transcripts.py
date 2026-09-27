from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.entities import Object, Project, Segment, Transcript, TranscriptSource, User, Video
from app.schemas.transcript import (
    AutoTranscriptionRequest,
    AutoTranscriptionResponse,
    TranscriptIngestRequest,
    TranscriptIngestResponse,
    TranscriptResponse,
    TranscriptUpdate,
    VideoTranscriptDetailResponse,
)
from app.services.job_queue import enqueue_indexing, enqueue_transcription
from app.services.audit_events import append_audit_event, append_metadata_change_event, append_publication_state_event, build_user_actor_payload
from app.services.transcript_parser import parse_transcript

router = APIRouter(prefix="/transcripts", tags=["transcripts"])


def _normalize_transcript_target_name(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.strip().lower()
    if normalized.endswith(" transcript"):
        normalized = normalized[: -len(" transcript")]
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[0]
    return "".join(character for character in normalized if character.isalnum())


def _guard_transcript_target_drift(db: Session, owner_id: UUID, video: Video, transcript_title: str | None) -> None:
    normalized_title = _normalize_transcript_target_name(transcript_title)
    if not normalized_title:
        return

    selected_names = {
        _normalize_transcript_target_name(video.title),
        _normalize_transcript_target_name(video.original_filename),
    }
    if normalized_title in selected_names:
        return

    other_videos = db.execute(
        select(Video)
        .join(Project, Project.id == Video.project_id)
        .where(Project.owner_id == owner_id, Video.id != video.id)
    ).scalars().all()

    for candidate in other_videos:
        candidate_names = {
            _normalize_transcript_target_name(candidate.title),
            _normalize_transcript_target_name(candidate.original_filename),
        }
        if normalized_title in candidate_names:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f'Transcript title appears to match video "{candidate.title}" rather than the selected '
                    f'video "{video.title}". Re-select the correct video before ingesting.'
                ),
            )


def _owned_video(db: Session, video_id: UUID, owner_id: UUID) -> Video | None:
    return db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


@router.get("/videos/{video_id}", response_model=VideoTranscriptDetailResponse)
def get_video_transcript(
    video_id: UUID,
    published_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VideoTranscriptDetailResponse:
    video = _owned_video(db, video_id, current_user.id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    transcript_query = (
        select(Transcript)
        .where(Transcript.video_id == video.id)
        .order_by(
            case((Transcript.source == TranscriptSource.MANUAL, 0), else_=1),
            Transcript.updated_at.desc(),
        )
    )
    if published_only:
        transcript_query = transcript_query.where(Transcript.is_published.is_(True))
    transcript = db.execute(transcript_query).scalars().first()
    if transcript is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcript not found")

    segments = db.execute(
        select(Segment)
        .where(Segment.transcript_id == transcript.id)
        .order_by(Segment.position.asc())
    ).scalars().all()

    return VideoTranscriptDetailResponse(transcript=transcript, segments=segments)


@router.get("", response_model=list[TranscriptResponse])
def list_transcripts(
    video_id: UUID | None = None,
    published_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Transcript]:
    query = (
        select(Transcript)
        .join(Video, Video.id == Transcript.video_id)
        .join(Project, Project.id == Video.project_id)
        .where(Project.owner_id == current_user.id)
    )
    if video_id is not None:
        query = query.where(Transcript.video_id == video_id)
    if published_only:
        query = query.where(Transcript.is_published.is_(True))
    return db.execute(query.order_by(Transcript.updated_at.desc())).scalars().all()


@router.post("", response_model=TranscriptIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_transcript(
    payload: TranscriptIngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TranscriptIngestResponse:
    video = _owned_video(db, payload.video_id, current_user.id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    _guard_transcript_target_drift(db, current_user.id, video, payload.title)

    try:
        parsed_segments = parse_transcript(payload.raw_text, payload.format)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    transcript = db.execute(
        select(Transcript).where(Transcript.video_id == video.id, Transcript.source == TranscriptSource.MANUAL)
    ).scalar_one_or_none()

    if transcript is None:
        transcript = Transcript(
            video_id=video.id,
            title=(payload.title or f"{video.title} Transcript").strip(),
            source=TranscriptSource.MANUAL,
            language=payload.language,
            format=payload.format,
            raw_text=payload.raw_text,
        )
        db.add(transcript)
        db.flush()
    else:
        if payload.title is not None:
            transcript.title = payload.title.strip()
        transcript.language = payload.language
        transcript.format = payload.format
        transcript.raw_text = payload.raw_text
        db.add(transcript)
        db.flush()

        db.execute(delete(Segment).where(Segment.transcript_id == transcript.id))

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

    db.commit()
    db.refresh(transcript)

    indexing_job_id = enqueue_indexing(transcript.id)
    object_row = db.get(Object, video.object_id) if video.object_id else None
    actor_json = build_user_actor_payload(current_user, trigger="api.transcripts.ingest")
    append_audit_event(
        db,
        event_type="transcript.reindex_requested",
        subject_type="transcript",
        subject_id=transcript.id,
        actor_json=actor_json,
        root_public_id=object_row.website_object_id if object_row is not None else None,
        payload_json={
            "segments_indexed": len(parsed_segments),
            "indexing_job_id": indexing_job_id,
            "source": transcript.source.value,
        },
    )
    db.commit()

    return TranscriptIngestResponse(
        transcript=transcript,
        segments_indexed=len(parsed_segments),
        indexing_job_id=indexing_job_id,
    )


@router.patch("/{transcript_id}", response_model=TranscriptResponse)
def update_transcript(
    transcript_id: UUID,
    payload: TranscriptUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Transcript:
    transcript = db.execute(
        select(Transcript)
        .join(Video, Video.id == Transcript.video_id)
        .join(Project, Project.id == Video.project_id)
        .where(Transcript.id == transcript_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if transcript is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcript not found")

    updates = payload.model_dump(exclude_unset=True)
    previous_is_published = bool(transcript.is_published)
    previous_values = {"title": transcript.title}
    if "title" in updates:
        transcript.title = updates["title"].strip()
    if "is_published" in updates:
        transcript.is_published = bool(updates["is_published"])

    video = db.get(Video, transcript.video_id)
    object_row = db.get(Object, video.object_id) if video is not None and video.object_id else None
    actor_json = build_user_actor_payload(current_user, trigger="api.transcripts.update")
    append_publication_state_event(
        db,
        subject_type="transcript",
        subject_id=transcript.id,
        actor_json=actor_json,
        root_public_id=object_row.website_object_id if object_row is not None else None,
        previous_value=previous_is_published,
        next_value=bool(transcript.is_published),
    )
    append_metadata_change_event(
        db,
        subject_type="transcript",
        subject_id=transcript.id,
        actor_json=actor_json,
        root_public_id=object_row.website_object_id if object_row is not None else None,
        changes={
            key: {"before": previous_values[key], "after": getattr(transcript, key)}
            for key in previous_values
            if key in updates and previous_values[key] != getattr(transcript, key)
        },
    )

    db.add(transcript)
    db.commit()
    db.refresh(transcript)
    return transcript


@router.post("/videos/{video_id}/auto", response_model=AutoTranscriptionResponse)
def request_auto_transcription(
    video_id: UUID,
    payload: AutoTranscriptionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AutoTranscriptionResponse:
    video = _owned_video(db, video_id, current_user.id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    job_id = enqueue_transcription(video.id, language=payload.language)
    detail = "Auto-transcription queued" if job_id else "Auto-transcription request accepted, but queue is unavailable"
    return AutoTranscriptionResponse(detail=detail, transcription_job_id=job_id)
