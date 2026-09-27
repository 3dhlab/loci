from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import AnnotationReviewStatus, TranscriptFormat, TranscriptSource, VideoStatus
from app.schemas.object_model import CameraViewPayload, ObjectModelAnnotationPlaylistEntry


class PublicStatsResponse(BaseModel):
    """Public-collection stats for the landing page and /public stats panel.

    open_now_count        Objects passing the full publish cascade (object +
                          model + video READY + transcript + annotations all
                          published) — i.e. browsable on /public and reachable
                          via /evidence/objects/{slug}.

    in_preparation_count  Objects with object.is_published=True at the
                          object level but missing one or more cascade gates.
                          When zero, the landing/public surface stays
                          honestly quiet; when positive, surfaces should
                          pair with a notify-me capture per Contract 2 of
                          the public subscription interface.
    """

    open_now_count: int = Field(ge=0)
    in_preparation_count: int = Field(ge=0)


class PublicProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class PublicObjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    description: str | None
    public_standfirst: str | None
    poster_url: str | None = None
    external_url: str | None
    evidence_url: str | None = None
    is_published: bool
    created_at: datetime
    updated_at: datetime


class PublicObjectPageResponse(BaseModel):
    items: list[PublicObjectResponse]
    page: int = Field(ge=1)
    page_size: Literal[3] = 3
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class PublicVideoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    object_id: UUID | None
    stable_video_id: str
    title: str
    original_filename: str
    duration_ms: int | None
    status: VideoStatus
    is_published: bool
    created_at: datetime
    updated_at: datetime


class PublicTranscriptResponse(BaseModel):
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


class PublicSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transcript_id: UUID
    position: int
    start_ms: int
    end_ms: int
    text: str
    created_at: datetime


class PublicModelDeliveryTierResponse(BaseModel):
    client_category: Literal["standard", "constrained"]
    variant: Literal["mobile-1024", "mobile-2048", "mobile-4096", "web-8192"]


class PublicModelDeliveryCapabilitiesResponse(BaseModel):
    exact_required: Literal[True] = True
    tiers: list[PublicModelDeliveryTierResponse] = Field(min_length=2, max_length=2)


class PublicObjectModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    object_id: UUID
    original_filename: str
    mime_type: str
    revision_number: int
    position_x: float
    position_y: float
    position_z: float
    rotation_x: float
    rotation_y: float
    rotation_z: float
    default_camera_json: CameraViewPayload | None = None
    delivery_capabilities: PublicModelDeliveryCapabilitiesResponse | None = None
    is_published: bool
    created_at: datetime
    updated_at: datetime


class PublicObjectModelAnnotationResponse(BaseModel):
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
    evidence_url: str | None = None
    review_status: AnnotationReviewStatus
    is_published: bool
    model_revision_created_against: int
    created_at: datetime
    updated_at: datetime


class PublicObjectPreviewResponse(BaseModel):
    object: PublicObjectResponse
    model: PublicObjectModelResponse | None = None
    model_url: str = ""
    annotations: list[PublicObjectModelAnnotationResponse]
    videos: list[PublicVideoResponse]
    video: PublicVideoResponse | None = None
    video_url: str = ""
    transcript: PublicTranscriptResponse | None = None
    segments: list[PublicSegmentResponse]


class PublicEvidencePageFrameResponse(BaseModel):
    title: str
    summary: str | None = None


class PublicEvidenceObjectSummaryResponse(BaseModel):
    id: str
    title: str
    summary: str | None = None
    poster_url: str | None = None
    external_url: str | None = None
    project_slug: str | None = None
    project_name: str | None = None


class PublicEvidenceFocusResponse(BaseModel):
    source: Literal["clip", "annotation", "video", "t", "default"]
    object_id: str
    annotation_id: str | None = None
    clip_id: str | None = None
    video_id: str
    seek_ms: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class PublicEvidenceModelResponse(BaseModel):
    model_url: str
    delivery_capabilities: PublicModelDeliveryCapabilitiesResponse | None = None
    default_camera: CameraViewPayload | None = None
    selected_camera: CameraViewPayload | None = None


class PublicEvidencePlaybackResponse(BaseModel):
    video_id: str
    video_title: str
    video_stream_url: str
    seek_ms: int = Field(ge=0)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int | None = Field(default=None, ge=0)
    # Total playable length, already known from the published video row. Plain
    # object routes defer the video body entirely, so without this the timeline has
    # no length to render until the visitor starts playback. Nullable because older
    # rows may predate duration probing. Already public on PublicVideoResponse.
    duration_ms: int | None = Field(default=None, ge=0)


class PublicEvidenceAnnotationResponse(BaseModel):
    id: str
    title: str
    description: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    point_x: float
    point_y: float
    point_z: float
    normal_x: float | None = None
    normal_y: float | None = None
    normal_z: float | None = None
    camera: CameraViewPayload | None = None
    related_clip_ids: list[str] = Field(default_factory=list)


class PublicEvidenceClipSummaryResponse(BaseModel):
    id: str
    title: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class PublicEvidenceClipDetailResponse(PublicEvidenceClipSummaryResponse):
    stream_url: str
    poster_url: str
    transcript_excerpt: str | None = None


class PublicEvidenceTranscriptSegmentResponse(BaseModel):
    position: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class PublicEvidenceTranscriptResponse(BaseModel):
    video_id: str
    segments: list[PublicEvidenceTranscriptSegmentResponse] = Field(default_factory=list)


class PublicEvidenceCitationAttributionResponse(BaseModel):
    speaker_label: str | None = None
    session_date: date | None = None
    session_date_text: str | None = None
    session_date_precision: Literal["day", "text", "unknown"] = "unknown"
    attribution_mode: Literal["named", "unavailable"] = "unavailable"


class PublicEvidenceCitationAttributionTimelineEntryResponse(BaseModel):
    video_id: str
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    speaker_label: str | None = None
    session_date: date | None = None
    session_date_text: str | None = None


class PublicEvidencePageResponse(BaseModel):
    canonical_url: str
    page: PublicEvidencePageFrameResponse
    object: PublicEvidenceObjectSummaryResponse
    focus: PublicEvidenceFocusResponse
    model: PublicEvidenceModelResponse
    playback: PublicEvidencePlaybackResponse
    selected_annotation: PublicEvidenceAnnotationResponse | None = None
    selected_clip: PublicEvidenceClipDetailResponse | None = None
    annotations: list[PublicEvidenceAnnotationResponse] = Field(default_factory=list)
    clips: list[PublicEvidenceClipSummaryResponse] = Field(default_factory=list)
    transcript: PublicEvidenceTranscriptResponse | None = None
    citation_attribution: PublicEvidenceCitationAttributionResponse | None = None
    citation_attribution_timeline: list[PublicEvidenceCitationAttributionTimelineEntryResponse] = Field(default_factory=list)
