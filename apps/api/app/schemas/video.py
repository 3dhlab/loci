from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.entities import VideoStatus


class VideoCreate(BaseModel):
    project_id: UUID
    object_id: UUID | None = None
    stable_video_id: str
    title: str
    original_filename: str | None = None
    source_path: str
    sha256_checksum: str | None = None
    duration_ms: int | None = None
    enqueue_transcode: bool = True
    auto_transcribe_if_missing: bool = True
    transcription_language: str = "en"


class VideoUpdate(BaseModel):
    object_id: UUID | None = None
    title: str | None = None
    original_filename: str | None = None
    source_path: str | None = None
    duration_ms: int | None = None
    status: VideoStatus | None = None
    is_published: bool | None = None


class VideoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    object_id: UUID | None
    stable_video_id: str
    title: str
    original_filename: str
    source_path: str
    sha256_checksum: str
    duration_ms: int | None
    status: VideoStatus
    transcode_progress_pct: int | None
    transcode_stage: str | None
    transcode_started_at: datetime | None
    is_published: bool
    created_at: datetime
    updated_at: datetime


class VideoCreateResponse(BaseModel):
    video: VideoResponse
    transcode_job_id: str | None
    transcription_job_id: str | None


class MediaFileResponse(BaseModel):
    path: str
    filename: str
    size_bytes: int
