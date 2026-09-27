from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import AnnotationReviewStatus


class CameraViewPayload(BaseModel):
    position: list[float] = Field(min_length=3, max_length=3)
    target: list[float] = Field(min_length=3, max_length=3)


class ObjectModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    object_id: UUID
    storage_path: str
    original_filename: str
    mime_type: str
    sha256_checksum: str
    file_size_bytes: int
    revision_number: int
    position_x: float
    position_y: float
    position_z: float
    rotation_x: float
    rotation_y: float
    rotation_z: float
    default_camera_json: CameraViewPayload | None = None
    is_published: bool
    # per-package "Notify followers on publish" toggle. When True,
    # a successful publish enqueues a digest email to subscribers in the
    # same customer_key partition. Default False (opt-in).
    notify_followers_on_publish: bool = False
    uploaded_by: UUID
    created_at: datetime
    updated_at: datetime


class ObjectModelSettingsUpdate(BaseModel):
    position_x: float | None = None
    position_y: float | None = None
    position_z: float | None = None
    rotation_x: float | None = None
    rotation_y: float | None = None
    rotation_z: float | None = None
    default_camera_json: CameraViewPayload | None = None
    is_published: bool | None = None
    notify_followers_on_publish: bool | None = None


class ObjectModelAnnotationPlaylistEntry(BaseModel):
    video_id: UUID
    clip_id: UUID | None = None
    transcript_segment_id: UUID | None = None
    label: str | None = Field(default=None, max_length=255)
    start_ms: int
    end_ms: int


class ObjectModelAnnotationCreate(BaseModel):
    video_id: UUID
    clip_id: UUID | None = None
    transcript_segment_id: UUID | None = None
    title: str
    description: str | None = None
    point_x: float
    point_y: float
    point_z: float
    normal_x: float | None = None
    normal_y: float | None = None
    normal_z: float | None = None
    camera_json: dict | None = None
    start_ms: int
    end_ms: int
    playlist: list[ObjectModelAnnotationPlaylistEntry] | None = None


class ObjectModelAnnotationUpdate(BaseModel):
    video_id: UUID | None = None
    clip_id: UUID | None = None
    transcript_segment_id: UUID | None = None
    title: str | None = None
    description: str | None = None
    point_x: float | None = None
    point_y: float | None = None
    point_z: float | None = None
    normal_x: float | None = None
    normal_y: float | None = None
    normal_z: float | None = None
    camera_json: dict | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    playlist: list[ObjectModelAnnotationPlaylistEntry] | None = None
    review_status: AnnotationReviewStatus | None = None
    is_published: bool | None = None


class ObjectModelAnnotationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    object_model_id: UUID
    object_id: UUID
    video_id: UUID
    clip_id: UUID | None
    transcript_segment_id: UUID | None
    title: str
    description: str | None
    point_x: float
    point_y: float
    point_z: float
    normal_x: float | None
    normal_y: float | None
    normal_z: float | None
    camera_json: dict | None
    playlist: list[ObjectModelAnnotationPlaylistEntry] = Field(default_factory=list)
    start_ms: int
    end_ms: int
    review_status: AnnotationReviewStatus
    is_published: bool
    model_revision_created_against: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime
