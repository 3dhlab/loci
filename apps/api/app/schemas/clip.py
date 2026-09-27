from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import ClipStatus


class ClipCreateRequest(BaseModel):
    video_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class ClipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: UUID
    project_id: UUID
    start_ms: int
    end_ms: int
    output_mp4_path: str | None
    metadata_json_path: str | None
    transcript_excerpt: str | None
    citation_text: str | None
    status: ClipStatus
    created_at: datetime
    updated_at: datetime


class ClipCreateResponse(BaseModel):
    clip: ClipResponse
    export_job_id: str | None


class ClipListResponse(BaseModel):
    clips: list[ClipResponse]
