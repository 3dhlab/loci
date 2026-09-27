from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class VisualSampleFrame(BaseModel):
    sample_index: int
    timestamp_ms: int
    asset_url: str | None = None


class SegmentSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    project_id: UUID | None = None
    video_id: UUID | None = None
    published_only: bool = False
    retrieval_mode: Literal["transcript_only", "visual_only", "combined"] = "transcript_only"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=25)
    limit: int | None = Field(default=None, ge=1, le=100)


class SegmentSearchResult(BaseModel):
    segment_id: UUID
    transcript_id: UUID
    transcript_window_id: UUID | None = None
    video_id: UUID
    video_title: str
    project_id: UUID
    object_id: UUID | None = None
    object_name: str | None = None
    object_public_id: str | None = None
    project_name: str | None = None
    project_slug: str | None = None
    stable_video_id: str | None = None
    evidence_url: str | None = None
    start_ms: int
    end_ms: int
    context_start_ms: int | None = None
    context_end_ms: int | None = None
    text: str
    lexical_match: bool
    semantic_score: float | None = None
    visual_score: float | None = None
    thumbnail_path: str | None = None
    thumbnail_url: str | None = None
    scene_description: str | None = None
    sample_frames: list[VisualSampleFrame] = Field(default_factory=list)
    rank_score: float


class SegmentSearchResponse(BaseModel):
    search_log_id: UUID | None = None
    query: str
    retrieval_mode: str | None = None
    total_results: int
    page: int
    page_size: int
    total_pages: int
    result_window: int
    result_window_capped: bool
    results: list[SegmentSearchResult]


class SearchResultFeedbackRequest(BaseModel):
    search_query_log_id: UUID
    segment_id: UUID
    is_relevant: bool
    lexical_match: bool
    semantic_score: float | None = None
    rank_score: float
    rank_position: int = Field(ge=1)


class SearchResultFeedbackResponse(BaseModel):
    id: UUID
    search_query_log_id: UUID
    segment_id: UUID
    is_relevant: bool
    lexical_match: bool
    semantic_score: float | None = None
    rank_score: float
    rank_position: int


class SearchTuningModeBreakdown(BaseModel):
    label: str
    vote_count: int


class SearchTuningCanonicalQuerySummary(BaseModel):
    canonical_query: str
    variants: list[str]
    vote_count: int
    positive_vote_count: int
    negative_vote_count: int
    approval_rate: float
    distinct_query_run_count: int
    is_recurring: bool
    average_semantic_score: float | None = None
    average_rank_position: float | None = None
    retrieval_modes: list[SearchTuningModeBreakdown]


class SearchTuningScoreBandSummary(BaseModel):
    label: str
    vote_count: int
    positive_vote_count: int
    negative_vote_count: int
    approval_rate: float


class SearchTuningRetrievalModeSummary(BaseModel):
    label: str
    vote_count: int
    positive_vote_count: int
    negative_vote_count: int
    approval_rate: float


class SearchTuningRecentFeedbackItem(BaseModel):
    created_at: datetime
    query_text: str
    canonical_query: str
    is_relevant: bool
    rank_position: int
    semantic_score: float | None = None
    lexical_match: bool
    retrieval_mode: str


class SearchTuningWindowSummaryResponse(BaseModel):
    feedback_window_start_at: datetime | None = None
    feedback_window_end_at: datetime | None = None
    new_vote_count: int
    positive_vote_count: int
    negative_vote_count: int
    canonical_query_count: int
    recurring_canonical_query_count: int
    ready_for_report: bool
    minimum_new_votes_required: int
    minimum_recurring_canonical_queries_required: int
    canonical_queries: list[SearchTuningCanonicalQuerySummary]
    score_bands: list[SearchTuningScoreBandSummary]
    retrieval_modes: list[SearchTuningRetrievalModeSummary]
    recent_feedback: list[SearchTuningRecentFeedbackItem]


class SearchTuningReportResponse(BaseModel):
    id: UUID
    status: str
    feedback_window_start_at: datetime | None = None
    feedback_window_end_at: datetime
    new_vote_count: int
    canonical_query_count: int
    recurring_canonical_query_count: int
    positive_vote_count: int
    negative_vote_count: int
    created_at: datetime
    approved_at: datetime | None = None
    summary: SearchTuningWindowSummaryResponse


class SearchTuningSummaryResponse(BaseModel):
    live_window: SearchTuningWindowSummaryResponse
    latest_report: SearchTuningReportResponse | None = None


class SuggestItem(BaseModel):
    phrase_text: str
    source: str
    score: float


class SuggestResponse(BaseModel):
    query: str
    results: list[SuggestItem]


class CorpusPhraseRebuildRequest(BaseModel):
    project_id: UUID


class CorpusPhraseRebuildResponse(BaseModel):
    enqueued: bool
    job_id: str | None = None
    project_id: UUID
