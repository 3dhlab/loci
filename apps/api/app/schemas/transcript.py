from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import TranscriptFormat, TranscriptSource


class TranscriptIngestRequest(BaseModel):
    video_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=255)
    format: TranscriptFormat
    raw_text: str = Field(min_length=1)
    language: str = Field(default="en", min_length=2, max_length=32)


class TranscriptUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    is_published: bool | None = None


class TranscriptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: UUID
    title: str
    source: TranscriptSource
    language: str
    format: TranscriptFormat
    raw_text: str
    is_published: bool
    created_at: datetime
    updated_at: datetime


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transcript_id: UUID
    position: int
    start_ms: int
    end_ms: int
    text: str
    created_at: datetime


class TranscriptIngestResponse(BaseModel):
    transcript: TranscriptResponse
    segments_indexed: int
    indexing_job_id: str | None


class VideoTranscriptDetailResponse(BaseModel):
    transcript: TranscriptResponse
    segments: list[SegmentResponse]


class AutoTranscriptionRequest(BaseModel):
    language: str = Field(default="en", min_length=2, max_length=32)


class AutoTranscriptionResponse(BaseModel):
    detail: str
    transcription_job_id: str | None
