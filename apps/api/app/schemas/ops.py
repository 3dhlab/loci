from pydantic import BaseModel


class LiveDatabaseSyncResponse(BaseModel):
    detail: str
    generated_at: str | None = None
    project_count: int
    object_count: int
    video_count: int
    transcript_count: int
    annotation_count: int
    media_file_count: int


class LiveDatabaseSyncStatusResponse(BaseModel):
    operation_id: str | None = None
    state: str
    stage_key: str | None = None
    stage_label: str | None = None
    detail: str
    progress_percent: int
    started_at: str | None = None
    finished_at: str | None = None
    generated_at: str | None = None
    project_count: int = 0
    object_count: int = 0
    video_count: int = 0
    transcript_count: int = 0
    annotation_count: int = 0
    media_file_count: int = 0
    error: str | None = None
