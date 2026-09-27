import mimetypes
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_stream
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import (
    CorpusPhrase,
    Object,
    Project,
    SearchQueryLog,
    SearchResultFeedback,
    SearchTuningReport,
    Segment,
    Transcript,
    TranscriptWindow,
    TuningReportStatus,
    User,
    VisualWindowDescription,
    Video,
)
from app.schemas.search import (
    CorpusPhraseRebuildRequest,
    CorpusPhraseRebuildResponse,
    SearchResultFeedbackRequest,
    SearchResultFeedbackResponse,
    SearchTuningCanonicalQuerySummary,
    SearchTuningModeBreakdown,
    SearchTuningRecentFeedbackItem,
    SearchTuningReportResponse,
    SearchTuningRetrievalModeSummary,
    SearchTuningScoreBandSummary,
    SearchTuningSummaryResponse,
    SearchTuningWindowSummaryResponse,
    SegmentSearchRequest,
    SegmentSearchResponse,
    SegmentSearchResult,
    SuggestItem,
    SuggestResponse,
    VisualSampleFrame,
)
from app.services.ai import AIProviderError, get_embedding_provider, warm_embedding_provider
from app.services.job_queue import enqueue_corpus_phrase_build
from app.services.search_tuning import build_tuning_window_summary, latest_tuning_report, serialize_tuning_window_summary

router = APIRouter(prefix="/search", tags=["search"])

MAX_SEARCH_RESULT_WINDOW = 100
DEFAULT_PAGE_SIZE = 25
MIN_ANALYSIS_SEMANTIC_RESULT_SCORE = 0.68
MIN_PUBLIC_SEMANTIC_RESULT_SCORE = 0.68


@dataclass
class SemanticRetrievalSummary:
    rows: list
    retrieval_mode: str | None = None
    top_window_score: float | None = None
    top_surfaced_segment_score: float | None = None


def _window_summary_response(summary_dict: dict) -> SearchTuningWindowSummaryResponse:
    return SearchTuningWindowSummaryResponse(
        feedback_window_start_at=summary_dict.get("feedback_window_start_at"),
        feedback_window_end_at=summary_dict.get("feedback_window_end_at"),
        new_vote_count=summary_dict["new_vote_count"],
        positive_vote_count=summary_dict["positive_vote_count"],
        negative_vote_count=summary_dict["negative_vote_count"],
        canonical_query_count=summary_dict["canonical_query_count"],
        recurring_canonical_query_count=summary_dict["recurring_canonical_query_count"],
        ready_for_report=summary_dict["ready_for_report"],
        minimum_new_votes_required=summary_dict["minimum_new_votes_required"],
        minimum_recurring_canonical_queries_required=summary_dict["minimum_recurring_canonical_queries_required"],
        canonical_queries=[
            SearchTuningCanonicalQuerySummary(
                canonical_query=item["canonical_query"],
                variants=item["variants"],
                vote_count=item["vote_count"],
                positive_vote_count=item["positive_vote_count"],
                negative_vote_count=item["negative_vote_count"],
                approval_rate=item["approval_rate"],
                distinct_query_run_count=item["distinct_query_run_count"],
                is_recurring=item["is_recurring"],
                average_semantic_score=item["average_semantic_score"],
                average_rank_position=item["average_rank_position"],
                retrieval_modes=[
                    SearchTuningModeBreakdown(label=mode["label"], vote_count=mode["vote_count"])
                    for mode in item["retrieval_modes"]
                ],
            )
            for item in summary_dict["canonical_queries"]
        ],
        score_bands=[
            SearchTuningScoreBandSummary(
                label=item["label"],
                vote_count=item["vote_count"],
                positive_vote_count=item["positive_vote_count"],
                negative_vote_count=item["negative_vote_count"],
                approval_rate=item["approval_rate"],
            )
            for item in summary_dict["score_bands"]
        ],
        retrieval_modes=[
            SearchTuningRetrievalModeSummary(
                label=item["label"],
                vote_count=item["vote_count"],
                positive_vote_count=item["positive_vote_count"],
                negative_vote_count=item["negative_vote_count"],
                approval_rate=item["approval_rate"],
            )
            for item in summary_dict["retrieval_modes"]
        ],
        recent_feedback=[
            SearchTuningRecentFeedbackItem(
                created_at=item["created_at"],
                query_text=item["query_text"],
                canonical_query=item["canonical_query"],
                is_relevant=item["is_relevant"],
                rank_position=item["rank_position"],
                semantic_score=item["semantic_score"],
                lexical_match=item["lexical_match"],
                retrieval_mode=item["retrieval_mode"],
            )
            for item in summary_dict["recent_feedback"]
        ],
    )


def _report_response(report: SearchTuningReport) -> SearchTuningReportResponse:
    return SearchTuningReportResponse(
        id=report.id,
        status=report.status.value if hasattr(report.status, "value") else str(report.status),
        feedback_window_start_at=report.feedback_window_start_at,
        feedback_window_end_at=report.feedback_window_end_at,
        new_vote_count=report.new_vote_count,
        canonical_query_count=report.canonical_query_count,
        recurring_canonical_query_count=report.recurring_canonical_query_count,
        positive_vote_count=report.positive_vote_count,
        negative_vote_count=report.negative_vote_count,
        created_at=report.created_at,
        approved_at=report.approved_at,
        summary=_window_summary_response(report.summary_json),
    )


def _segment_result(
    segment: Segment,
    transcript: Transcript,
    video: Video,
    *,
    lexical_match: bool,
    semantic_score: float | None,
    rank_score: float,
    transcript_window_id: UUID | None = None,
    context_start_ms: int | None = None,
    context_end_ms: int | None = None,
    visual_score: float | None = None,
    thumbnail_path: str | None = None,
    thumbnail_url: str | None = None,
    scene_description: str | None = None,
    sample_frames: list[VisualSampleFrame] | None = None,
) -> SegmentSearchResult:
    return SegmentSearchResult(
        segment_id=segment.id,
        transcript_id=transcript.id,
        transcript_window_id=transcript_window_id,
        video_id=video.id,
        video_title=video.title,
        project_id=video.project_id,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        context_start_ms=context_start_ms,
        context_end_ms=context_end_ms,
        text=segment.text,
        lexical_match=lexical_match,
        semantic_score=semantic_score,
        visual_score=visual_score,
        thumbnail_path=thumbnail_path,
        thumbnail_url=thumbnail_url,
        scene_description=scene_description,
        sample_frames=sample_frames or [],
        rank_score=rank_score,
    )


def _lexical_pattern(query: str) -> str | None:
    tokens = [re.escape(token) for token in re.findall(r"\S+", query.strip())]
    if not tokens:
        return None
    phrase = r"\s+".join(tokens)
    return rf"(^|[^[:alnum:]_]){phrase}([^[:alnum:]_]|$)"


def _merge_semantic_result(
    result_map: dict[UUID, SegmentSearchResult],
    segment: Segment,
    transcript: Transcript,
    video: Video,
    semantic_score: float,
    *,
    transcript_window_id: UUID | None = None,
    context_start_ms: int | None = None,
    context_end_ms: int | None = None,
    visual_score: float | None = None,
    thumbnail_path: str | None = None,
) -> None:
    existing = result_map.get(segment.id)
    semantic_rank_score = semantic_score + (1.0 if existing and existing.lexical_match else 0.0)

    if existing:
        should_update_context = semantic_rank_score >= existing.rank_score or existing.transcript_window_id is None
        existing.semantic_score = max(existing.semantic_score or 0.0, semantic_score)
        existing.rank_score = max(existing.rank_score, semantic_rank_score)
        if should_update_context and transcript_window_id is not None:
            existing.transcript_window_id = transcript_window_id
            existing.context_start_ms = context_start_ms
            existing.context_end_ms = context_end_ms
        if visual_score is not None and (existing.visual_score is None or visual_score >= existing.visual_score):
            existing.visual_score = visual_score
        if thumbnail_path and (existing.thumbnail_path is None or should_update_context):
            existing.thumbnail_path = thumbnail_path
        return

    result_map[segment.id] = _segment_result(
        segment,
        transcript,
        video,
        lexical_match=False,
        semantic_score=semantic_score,
        rank_score=semantic_rank_score,
        transcript_window_id=transcript_window_id,
        context_start_ms=context_start_ms,
        context_end_ms=context_end_ms,
        visual_score=visual_score,
        thumbnail_path=thumbnail_path,
    )


def _visual_asset_path(transcript_window_id: UUID, sample_index: int | None = None) -> str:
    if sample_index is None:
        return f"/api/v1/search/visual-windows/{transcript_window_id}/thumbnail"
    return f"/api/v1/search/visual-windows/{transcript_window_id}/frames/{sample_index}"


def _visual_sample_frames(visual_row: VisualWindowDescription) -> list[VisualSampleFrame]:
    manifest = visual_row.frame_manifest_json or {}
    frames = manifest.get("sample_frames") or []
    serialized_frames: list[VisualSampleFrame] = []
    for entry in frames:
        sample_index = entry.get("sample_index")
        if sample_index is None:
            continue
        serialized_frames.append(
            VisualSampleFrame(
                sample_index=int(sample_index),
                timestamp_ms=max(0, int(entry.get("timestamp_ms") or 0)),
                asset_url=_visual_asset_path(visual_row.transcript_window_id, sample_index=int(sample_index)),
            )
        )
    return sorted(serialized_frames, key=lambda item: item.sample_index)


def _attach_transcript_window_context(db: Session, ranked_results: list[SegmentSearchResult]) -> None:
    unresolved_results = [item for item in ranked_results if item.transcript_window_id is None]
    if not unresolved_results:
        return

    segment_ids = [item.segment_id for item in unresolved_results]
    segment_rows = db.execute(
        select(Segment.id, Segment.transcript_id, Segment.position).where(Segment.id.in_(segment_ids))
    ).all()
    if not segment_rows:
        return

    segment_metadata = {
        segment_id: {
            "transcript_id": transcript_id,
            "position": position,
        }
        for segment_id, transcript_id, position in segment_rows
    }
    transcript_ids = {metadata["transcript_id"] for metadata in segment_metadata.values()}
    if not transcript_ids:
        return

    window_rows = db.execute(
        select(TranscriptWindow, VisualWindowDescription)
        .outerjoin(VisualWindowDescription, VisualWindowDescription.transcript_window_id == TranscriptWindow.id)
        .where(TranscriptWindow.transcript_id.in_(transcript_ids))
        .order_by(TranscriptWindow.transcript_id.asc(), TranscriptWindow.window_index.asc())
    ).all()
    windows_by_transcript: dict[UUID, list[tuple[TranscriptWindow, VisualWindowDescription | None]]] = {}
    for window, visual_row in window_rows:
        windows_by_transcript.setdefault(window.transcript_id, []).append((window, visual_row))

    for item in unresolved_results:
        metadata = segment_metadata.get(item.segment_id)
        if metadata is None:
            continue

        position = metadata["position"]
        candidates = [
            entry
            for entry in windows_by_transcript.get(metadata["transcript_id"], [])
            if entry[0].start_position <= position <= entry[0].end_position
        ]
        if not candidates:
            continue

        window, _ = min(
            candidates,
            key=lambda entry: (
                0
                if entry[1] is not None and entry[1].status == "ready" and bool(entry[1].thumbnail_path)
                else 1,
                0 if entry[1] is not None and entry[1].status == "ready" else 1,
                abs((entry[0].start_position + entry[0].end_position) - (position * 2)),
                entry[0].end_position - entry[0].start_position,
                entry[0].window_index,
            ),
        )
        item.transcript_window_id = window.id
        item.context_start_ms = window.start_ms
        item.context_end_ms = window.end_ms


def _attach_visual_result_details(db: Session, ranked_results: list[SegmentSearchResult]) -> None:
    _attach_transcript_window_context(db, ranked_results)

    transcript_window_ids = list({item.transcript_window_id for item in ranked_results if item.transcript_window_id is not None})
    if not transcript_window_ids:
        return

    visual_rows = db.execute(
        select(VisualWindowDescription).where(VisualWindowDescription.transcript_window_id.in_(transcript_window_ids))
    ).scalars().all()
    visual_by_window_id = {row.transcript_window_id: row for row in visual_rows}

    for item in ranked_results:
        if item.transcript_window_id is None:
            continue
        visual_row = visual_by_window_id.get(item.transcript_window_id)
        if visual_row is None:
            continue
        if item.thumbnail_path is None:
            item.thumbnail_path = visual_row.thumbnail_path
        item.thumbnail_url = _visual_asset_path(visual_row.transcript_window_id)
        item.scene_description = visual_row.description_text
        item.sample_frames = _visual_sample_frames(visual_row)


def _resolve_media_asset(path_value: str | None) -> Path | None:
    if not path_value:
        return None

    media_root = Path(settings.media_root).resolve()
    candidate = Path(path_value).resolve()
    try:
        candidate.relative_to(media_root)
    except ValueError:
        return None

    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _owned_visual_window_description(
    db: Session,
    *,
    transcript_window_id: UUID,
    owner_id: UUID,
) -> VisualWindowDescription:
    visual_row = db.execute(
        select(VisualWindowDescription)
        .join(Video, Video.id == VisualWindowDescription.video_id)
        .join(Project, Project.id == Video.project_id)
        .where(
            VisualWindowDescription.transcript_window_id == transcript_window_id,
            Project.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if visual_row is None:
        raise HTTPException(status_code=404, detail="Visual window description not found")
    return visual_row


def _visual_asset_response(path: Path) -> FileResponse:
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path=path, filename=path.name, media_type=media_type or "application/octet-stream")


def _attach_best_segments(
    db: Session,
    *,
    windows_by_transcript: dict[UUID, list[dict]],
    window_range_filters: list,
    embedding,
) -> None:
    if not window_range_filters:
        return

    candidate_rows = db.execute(
        select(Segment, Segment.embedding_vector.cosine_distance(embedding).label("distance"))
        .where(and_(Segment.embedding_vector.is_not(None), or_(*window_range_filters)))
    ).all()
    for segment, distance in candidate_rows:
        segment_score = 1.0 / (1.0 + float(distance))
        for entry in windows_by_transcript.get(segment.transcript_id, []):
            window = entry["window"]
            if segment.position < window.start_position or segment.position > window.end_position:
                continue
            current = entry["best_segment"]
            if current is None or segment_score > current["segment_score"] or (
                math.isclose(segment_score, current["segment_score"]) and segment.start_ms < current["segment"].start_ms
            ):
                entry["best_segment"] = {
                    "segment": segment,
                    "segment_score": segment_score,
                }

    fallback_rows = db.execute(
        select(Segment)
        .where(or_(*window_range_filters))
        .order_by(Segment.transcript_id.asc(), Segment.position.asc())
    ).scalars().all()
    for segment in fallback_rows:
        for entry in windows_by_transcript.get(segment.transcript_id, []):
            window = entry["window"]
            if segment.position < window.start_position or segment.position > window.end_position:
                continue
            if entry["fallback_segment"] is None:
                entry["fallback_segment"] = segment


def _apply_segment_semantic_results(
    db: Session,
    *,
    base_filters: list,
    embedding,
    result_window: int,
    result_map: dict[UUID, SegmentSearchResult],
) -> SemanticRetrievalSummary:
    semantic_stmt = (
        select(Segment, Transcript, Video, Segment.embedding_vector.cosine_distance(embedding).label("distance"))
        .join(Transcript, Transcript.id == Segment.transcript_id)
        .join(Video, Video.id == Transcript.video_id)
        .join(Project, Project.id == Video.project_id)
        .outerjoin(Object, Object.id == Video.object_id)
        .where(and_(*base_filters), Segment.embedding_vector.is_not(None))
        .order_by("distance")
        .limit(result_window)
    )
    semantic_rows = db.execute(semantic_stmt).all()

    for segment, transcript, video, distance in semantic_rows:
        semantic_score = 1.0 / (1.0 + float(distance))
        _merge_semantic_result(result_map, segment, transcript, video, semantic_score)

    top_segment_score = None
    if semantic_rows:
        top_segment_score = max(1.0 / (1.0 + float(distance)) for _, _, _, distance in semantic_rows)

    return SemanticRetrievalSummary(
        rows=semantic_rows,
        retrieval_mode="segment_fallback",
        top_surfaced_segment_score=top_segment_score,
    )


def _apply_window_semantic_results(
    db: Session,
    *,
    base_filters: list,
    embedding,
    result_window: int,
    result_map: dict[UUID, SegmentSearchResult],
) -> SemanticRetrievalSummary:
    window_stmt = (
        select(TranscriptWindow, Transcript, Video, TranscriptWindow.embedding_vector.cosine_distance(embedding).label("distance"))
        .join(Transcript, Transcript.id == TranscriptWindow.transcript_id)
        .join(Video, Video.id == Transcript.video_id)
        .join(Project, Project.id == Video.project_id)
        .outerjoin(Object, Object.id == Video.object_id)
        .where(and_(*base_filters), TranscriptWindow.embedding_vector.is_not(None))
        .order_by("distance")
        .limit(result_window)
    )
    window_rows = db.execute(window_stmt).all()
    if not window_rows:
        return SemanticRetrievalSummary(rows=[])

    window_entries = []
    window_range_filters = []
    windows_by_transcript: dict[UUID, list[dict]] = {}

    for window, transcript, video, distance in window_rows:
        entry = {
            "window": window,
            "transcript": transcript,
            "video": video,
            "window_score": 1.0 / (1.0 + float(distance)),
            "best_segment": None,
            "fallback_segment": None,
        }
        window_entries.append(entry)
        windows_by_transcript.setdefault(window.transcript_id, []).append(entry)
        window_range_filters.append(
            and_(
                Segment.transcript_id == window.transcript_id,
                Segment.position >= window.start_position,
                Segment.position <= window.end_position,
            )
        )

    _attach_best_segments(
        db,
        windows_by_transcript=windows_by_transcript,
        window_range_filters=window_range_filters,
        embedding=embedding,
    )

    for entry in window_entries:
        chosen_segment = entry["best_segment"]["segment"] if entry["best_segment"] else entry["fallback_segment"]
        if chosen_segment is None:
            continue

        segment_score = entry["best_segment"]["segment_score"] if entry["best_segment"] else None
        semantic_score = entry["window_score"]
        if segment_score is not None:
            semantic_score = max(entry["window_score"], (entry["window_score"] * 0.7) + (segment_score * 0.3))

        _merge_semantic_result(
            result_map,
            chosen_segment,
            entry["transcript"],
            entry["video"],
            semantic_score,
            transcript_window_id=entry["window"].id,
            context_start_ms=entry["window"].start_ms,
            context_end_ms=entry["window"].end_ms,
        )

    top_window_score = max(entry["window_score"] for entry in window_entries) if window_entries else None
    top_segment_score = max(
        (
            entry["best_segment"]["segment_score"]
            for entry in window_entries
            if entry["best_segment"] is not None
        ),
        default=None,
    )

    return SemanticRetrievalSummary(
        rows=window_rows,
        retrieval_mode="window_first",
        top_window_score=top_window_score,
        top_surfaced_segment_score=top_segment_score,
    )


def _apply_visual_semantic_results(
    db: Session,
    *,
    base_filters: list,
    embedding,
    result_window: int,
    result_map: dict[UUID, SegmentSearchResult],
) -> SemanticRetrievalSummary:
    visual_stmt = (
        select(
            VisualWindowDescription,
            TranscriptWindow,
            Transcript,
            Video,
            VisualWindowDescription.embedding_vector.cosine_distance(embedding).label("distance"),
        )
        .join(TranscriptWindow, TranscriptWindow.id == VisualWindowDescription.transcript_window_id)
        .join(Transcript, Transcript.id == VisualWindowDescription.transcript_id)
        .join(Video, Video.id == VisualWindowDescription.video_id)
        .join(Project, Project.id == Video.project_id)
        .outerjoin(Object, Object.id == Video.object_id)
        .where(
            and_(
                *base_filters,
                VisualWindowDescription.status == "ready",
                VisualWindowDescription.embedding_vector.is_not(None),
            )
        )
        .order_by("distance")
        .limit(result_window)
    )
    visual_rows = db.execute(visual_stmt).all()
    if not visual_rows:
        return SemanticRetrievalSummary(rows=[], retrieval_mode="visual_only")

    visual_entries = []
    window_range_filters = []
    windows_by_transcript: dict[UUID, list[dict]] = {}

    for visual_row, window, transcript, video, distance in visual_rows:
        entry = {
            "visual_row": visual_row,
            "window": window,
            "transcript": transcript,
            "video": video,
            "visual_score": 1.0 / (1.0 + float(distance)),
            "best_segment": None,
            "fallback_segment": None,
        }
        visual_entries.append(entry)
        windows_by_transcript.setdefault(window.transcript_id, []).append(entry)
        window_range_filters.append(
            and_(
                Segment.transcript_id == window.transcript_id,
                Segment.position >= window.start_position,
                Segment.position <= window.end_position,
            )
        )

    _attach_best_segments(
        db,
        windows_by_transcript=windows_by_transcript,
        window_range_filters=window_range_filters,
        embedding=embedding,
    )

    for entry in visual_entries:
        chosen_segment = entry["best_segment"]["segment"] if entry["best_segment"] else entry["fallback_segment"]
        if chosen_segment is None:
            continue

        _merge_semantic_result(
            result_map,
            chosen_segment,
            entry["transcript"],
            entry["video"],
            entry["visual_score"],
            transcript_window_id=entry["window"].id,
            context_start_ms=entry["window"].start_ms,
            context_end_ms=entry["window"].end_ms,
            visual_score=entry["visual_score"],
            thumbnail_path=entry["visual_row"].thumbnail_path,
        )

    top_segment_score = max(
        (
            entry["best_segment"]["segment_score"]
            for entry in visual_entries
            if entry["best_segment"] is not None
        ),
        default=None,
    )
    return SemanticRetrievalSummary(
        rows=visual_rows,
        retrieval_mode="visual_only",
        top_surfaced_segment_score=top_segment_score,
    )


def _apply_visual_rerank(
    db: Session,
    *,
    embedding,
    result_map: dict[UUID, SegmentSearchResult],
) -> int:
    transcript_window_ids = list(
        {item.transcript_window_id for item in result_map.values() if item.transcript_window_id is not None}
    )
    if not transcript_window_ids:
        return 0

    visual_rows = db.execute(
        select(
            VisualWindowDescription.transcript_window_id,
            VisualWindowDescription.thumbnail_path,
            VisualWindowDescription.embedding_vector.cosine_distance(embedding).label("distance"),
        )
        .where(
            VisualWindowDescription.transcript_window_id.in_(transcript_window_ids),
            VisualWindowDescription.status == "ready",
            VisualWindowDescription.embedding_vector.is_not(None),
        )
    ).all()

    visual_by_window = {
        transcript_window_id: {
            "visual_score": 1.0 / (1.0 + float(distance)),
            "thumbnail_path": thumbnail_path,
        }
        for transcript_window_id, thumbnail_path, distance in visual_rows
    }

    reranked_count = 0
    for item in result_map.values():
        if item.transcript_window_id is None or item.semantic_score is None:
            continue
        visual_payload = visual_by_window.get(item.transcript_window_id)
        if visual_payload is None:
            continue
        item.visual_score = visual_payload["visual_score"]
        item.thumbnail_path = visual_payload["thumbnail_path"] or item.thumbnail_path
        item.rank_score = item.rank_score * (1.0 + (settings.visual_rerank_alpha * item.visual_score))
        reranked_count += 1

    return reranked_count


@router.post("/warmup")
def warmup_search(
    current_user: User = Depends(get_current_user),
) -> dict[str, str | bool | None]:
    _ = current_user
    model_name = warm_embedding_provider()
    return {
        "warmed": True,
        "model": model_name,
    }


@router.post("/segments", response_model=SegmentSearchResponse)
def search_segments(
    payload: SegmentSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SegmentSearchResponse:
    base_filters = [Project.owner_id == current_user.id]
    if payload.project_id is not None:
        base_filters.append(Video.project_id == payload.project_id)
    if payload.video_id is not None:
        base_filters.append(Video.id == payload.video_id)
    if payload.published_only:
        base_filters.extend(
            [
                Video.object_id.is_not(None),
                Video.is_published.is_(True),
                Transcript.is_published.is_(True),
                Object.is_published.is_(True),
            ]
        )

    requested_page_size = payload.page_size
    page_size = min(max(int(requested_page_size), 1), DEFAULT_PAGE_SIZE)
    requested_page = max(int(payload.page), 1)
    result_window = min(max(int(payload.limit or MAX_SEARCH_RESULT_WINDOW), 1), MAX_SEARCH_RESULT_WINDOW)

    semantic_attempted = False
    semantic_succeeded = False
    semantic_provider_name: str | None = None
    semantic_model_name: str | None = None
    retrieval_mode: str | None = None
    top_window_score: float | None = None
    top_surfaced_segment_score: float | None = None
    search_log_id: UUID | None = None
    minimum_semantic_result_score = (
        settings.visual_only_min_score
        if payload.retrieval_mode == "visual_only"
        else (MIN_PUBLIC_SEMANTIC_RESULT_SCORE if payload.published_only else MIN_ANALYSIS_SEMANTIC_RESULT_SCORE)
    )
    lexical_pattern = _lexical_pattern(payload.query)
    lexical_condition = (
        Segment.text.op("~*")(lexical_pattern)
        if lexical_pattern
        else Segment.text.ilike(f"%{payload.query}%")
    )

    lexical_rows: list = []
    if payload.retrieval_mode != "visual_only":
        lexical_stmt = (
            select(Segment, Transcript, Video)
            .join(Transcript, Transcript.id == Segment.transcript_id)
            .join(Video, Video.id == Transcript.video_id)
            .join(Project, Project.id == Video.project_id)
            .outerjoin(Object, Object.id == Video.object_id)
            .where(and_(*base_filters), lexical_condition)
            .order_by(Video.updated_at.desc(), Segment.start_ms.asc())
            .limit(result_window)
        )
        lexical_rows = db.execute(lexical_stmt).all()

    result_map: dict[UUID, SegmentSearchResult] = {}
    for segment, transcript, video in lexical_rows:
        score = 1.0
        result_map[segment.id] = SegmentSearchResult(
            segment_id=segment.id,
            transcript_id=transcript.id,
            video_id=video.id,
            video_title=video.title,
            project_id=video.project_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text=segment.text,
            lexical_match=True,
            semantic_score=None,
            rank_score=score,
        )

    semantic_rows: list = []
    try:
        semantic_attempted = settings.embedding_enabled
        if not settings.embedding_enabled:
            raise AIProviderError("Embedding retrieval is disabled by configuration.")
        provider = get_embedding_provider()
        semantic_provider_name = getattr(provider, "provider_name", None)
        embedding_result = provider.embed_texts([payload.query])
        semantic_model_name = getattr(embedding_result, "model", None)
        embedding = embedding_result.vectors[0]
        if payload.retrieval_mode == "visual_only":
            retrieval_summary = _apply_visual_semantic_results(
                db,
                base_filters=base_filters,
                embedding=embedding,
                result_window=result_window,
                result_map=result_map,
            )
            semantic_rows = retrieval_summary.rows
            retrieval_mode = retrieval_summary.retrieval_mode
            top_window_score = retrieval_summary.top_window_score
            top_surfaced_segment_score = retrieval_summary.top_surfaced_segment_score
        else:
            retrieval_summary = _apply_window_semantic_results(
                db,
                base_filters=base_filters,
                embedding=embedding,
                result_window=result_window,
                result_map=result_map,
            )
            semantic_rows = retrieval_summary.rows
            retrieval_mode = retrieval_summary.retrieval_mode
            top_window_score = retrieval_summary.top_window_score
            top_surfaced_segment_score = retrieval_summary.top_surfaced_segment_score
            if not semantic_rows:
                retrieval_summary = _apply_segment_semantic_results(
                    db,
                    base_filters=base_filters,
                    embedding=embedding,
                    result_window=result_window,
                    result_map=result_map,
                )
                semantic_rows = retrieval_summary.rows
                retrieval_mode = retrieval_summary.retrieval_mode
                top_window_score = retrieval_summary.top_window_score
                top_surfaced_segment_score = retrieval_summary.top_surfaced_segment_score
            elif payload.retrieval_mode == "combined":
                _apply_visual_rerank(db, embedding=embedding, result_map=result_map)
                retrieval_mode = "combined"
        semantic_succeeded = True
    except AIProviderError:
        semantic_rows = []

    all_ranked = sorted(
        (
            item
            for item in result_map.values()
            if item.lexical_match or item.semantic_score is None or item.semantic_score >= minimum_semantic_result_score
        ),
        key=lambda item: item.rank_score,
        reverse=True,
    )
    result_window_capped = (
        len(lexical_rows) == result_window
        or len(semantic_rows) == result_window
        or len(all_ranked) > result_window
    )
    all_ranked = all_ranked[:result_window]
    total_results = len(all_ranked)
    total_pages = math.ceil(total_results / page_size) if total_results else 0
    page = min(requested_page, total_pages) if total_pages else 1
    page_start = (page - 1) * page_size
    page_end = page_start + page_size
    ranked = all_ranked[page_start:page_end]
    _attach_visual_result_details(db, ranked)

    lexical_result_count = sum(1 for item in all_ranked if item.lexical_match)
    semantic_result_count = sum(1 for item in all_ranked if item.semantic_score is not None)
    top_semantic_score = max((item.semantic_score for item in all_ranked if item.semantic_score is not None), default=None)

    try:
        search_log = SearchQueryLog(
            user_id=current_user.id,
            project_id=payload.project_id,
            video_id=payload.video_id,
            query_text=payload.query.strip(),
            query_source="public_preview" if payload.published_only else "analysis",
            result_count=total_results,
            lexical_result_count=lexical_result_count,
            semantic_result_count=semantic_result_count,
            semantic_attempted=semantic_attempted,
            semantic_succeeded=semantic_succeeded,
            semantic_provider=semantic_provider_name,
            semantic_model=semantic_model_name,
            retrieval_mode=retrieval_mode,
            top_semantic_score=(Decimal(str(top_semantic_score)) if top_semantic_score is not None else None),
            top_window_score=(Decimal(str(top_window_score)) if top_window_score is not None else None),
            top_surfaced_segment_score=(
                Decimal(str(top_surfaced_segment_score))
                if top_surfaced_segment_score is not None
                else None
            ),
        )
        db.add(search_log)
        db.commit()
        db.refresh(search_log)
        search_log_id = search_log.id
    except Exception:
        db.rollback()

    return SegmentSearchResponse(
        search_log_id=search_log_id,
        query=payload.query,
        retrieval_mode=retrieval_mode,
        total_results=total_results,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        result_window=result_window,
        result_window_capped=result_window_capped,
        results=ranked,
    )


@router.get("/visual-windows/{transcript_window_id}/thumbnail")
def get_visual_window_thumbnail(
    transcript_window_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_stream),
) -> FileResponse:
    visual_row = _owned_visual_window_description(db, transcript_window_id=transcript_window_id, owner_id=current_user.id)
    asset_path = _resolve_media_asset(visual_row.thumbnail_path)
    if asset_path is None:
        raise HTTPException(status_code=404, detail="Visual window thumbnail not found")
    return _visual_asset_response(asset_path)


@router.get("/visual-windows/{transcript_window_id}/frames/{sample_index}")
def get_visual_window_sample_frame(
    transcript_window_id: UUID,
    sample_index: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_stream),
) -> FileResponse:
    visual_row = _owned_visual_window_description(db, transcript_window_id=transcript_window_id, owner_id=current_user.id)
    manifest = visual_row.frame_manifest_json or {}
    sample_frames = manifest.get("sample_frames") or []
    sample_entry = next(
        (
            entry
            for entry in sample_frames
            if entry.get("sample_index") is not None and int(entry["sample_index"]) == sample_index
        ),
        None,
    )
    if sample_entry is None:
        raise HTTPException(status_code=404, detail="Visual sample frame not found")

    asset_path = _resolve_media_asset(sample_entry.get("path"))
    if asset_path is None:
        raise HTTPException(status_code=404, detail="Visual sample frame not found")
    return _visual_asset_response(asset_path)


@router.post("/feedback", response_model=SearchResultFeedbackResponse)
def submit_search_result_feedback(
    payload: SearchResultFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResultFeedbackResponse:
    query_log = db.get(SearchQueryLog, payload.search_query_log_id)
    if query_log is None or query_log.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Search log entry not found.")
    if query_log.query_source != "analysis":
        raise HTTPException(status_code=400, detail="Search feedback is only available for Analysis queries.")

    segment = db.get(Segment, payload.segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Search result segment not found.")

    feedback = (
        db.execute(
            select(SearchResultFeedback).where(
                SearchResultFeedback.search_query_log_id == payload.search_query_log_id,
                SearchResultFeedback.segment_id == payload.segment_id,
            )
        )
        .scalars()
        .first()
    )
    if feedback is None:
        feedback = SearchResultFeedback(
            user_id=current_user.id,
            search_query_log_id=payload.search_query_log_id,
            segment_id=payload.segment_id,
            is_relevant=payload.is_relevant,
            lexical_match=payload.lexical_match,
            semantic_score=(Decimal(str(payload.semantic_score)) if payload.semantic_score is not None else None),
            rank_score=Decimal(str(payload.rank_score)),
            rank_position=payload.rank_position,
        )
        db.add(feedback)
    else:
        feedback.is_relevant = payload.is_relevant
        feedback.lexical_match = payload.lexical_match
        feedback.semantic_score = Decimal(str(payload.semantic_score)) if payload.semantic_score is not None else None
        feedback.rank_score = Decimal(str(payload.rank_score))
        feedback.rank_position = payload.rank_position

    db.commit()
    db.refresh(feedback)

    return SearchResultFeedbackResponse(
        id=feedback.id,
        search_query_log_id=feedback.search_query_log_id,
        segment_id=feedback.segment_id,
        is_relevant=feedback.is_relevant,
        lexical_match=feedback.lexical_match,
        semantic_score=float(feedback.semantic_score) if feedback.semantic_score is not None else None,
        rank_score=float(feedback.rank_score),
        rank_position=feedback.rank_position,
    )


@router.get("/tuning/summary", response_model=SearchTuningSummaryResponse)
def get_search_tuning_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchTuningSummaryResponse:
    live_window = build_tuning_window_summary(db, user_id=current_user.id)
    live_window_payload = serialize_tuning_window_summary(live_window)
    latest_report = latest_tuning_report(db, current_user.id)

    return SearchTuningSummaryResponse(
        live_window=_window_summary_response(live_window_payload),
        latest_report=_report_response(latest_report) if latest_report is not None else None,
    )


@router.post("/tuning/reports", response_model=SearchTuningReportResponse)
def create_search_tuning_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchTuningReportResponse:
    live_window = build_tuning_window_summary(db, user_id=current_user.id)
    if not live_window.ready_for_report:
        raise HTTPException(
            status_code=400,
            detail=(
                "A tuning report is not ready yet. Wait for at least "
                f"{live_window.minimum_new_votes_required} new votes and "
                f"{live_window.minimum_recurring_canonical_queries_required} recurring canonical queries."
            ),
        )
    if live_window.feedback_window_end_at is None:
        raise HTTPException(status_code=400, detail="No new feedback is available for a tuning report.")

    latest_report = latest_tuning_report(db, current_user.id)
    if (
        latest_report is not None
        and latest_report.feedback_window_start_at == live_window.feedback_window_start_at
        and latest_report.feedback_window_end_at == live_window.feedback_window_end_at
    ):
        return _report_response(latest_report)

    summary_json = serialize_tuning_window_summary(live_window)
    report = SearchTuningReport(
        user_id=current_user.id,
        status=TuningReportStatus.DRAFT,
        feedback_window_start_at=live_window.feedback_window_start_at,
        feedback_window_end_at=live_window.feedback_window_end_at,
        new_vote_count=live_window.new_vote_count,
        canonical_query_count=live_window.canonical_query_count,
        recurring_canonical_query_count=live_window.recurring_canonical_query_count,
        positive_vote_count=live_window.positive_vote_count,
        negative_vote_count=live_window.negative_vote_count,
        summary_json=summary_json,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return _report_response(report)


@router.post("/tuning/reports/{report_id}/approve-live-comparison", response_model=SearchTuningReportResponse)
def approve_search_tuning_report_live_comparison(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchTuningReportResponse:
    report = db.get(SearchTuningReport, report_id)
    if report is None or report.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tuning report not found.")

    report.status = TuningReportStatus.APPROVED_FOR_LIVE_COMPARISON
    report.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    return _report_response(report)


MIN_SUGGEST_PREFIX_LENGTH = 2
DEFAULT_SUGGEST_LIMIT = 7
MAX_SUGGEST_LIMIT = 15
ENTITY_SOURCE = "entity"


def _normalized_source_count(source_count: int) -> float:
    if source_count <= 0:
        return 0.0
    return min(1.0, math.log1p(source_count) / math.log1p(50))


@router.get("/suggest", response_model=SuggestResponse)
def suggest_search_phrases(
    q: str = Query("", max_length=120),
    project_id: UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_SUGGEST_LIMIT, ge=1, le=MAX_SUGGEST_LIMIT),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuggestResponse:
    normalized_prefix = re.sub(r"\s+", " ", (q or "").strip().lower())
    if len(normalized_prefix) < MIN_SUGGEST_PREFIX_LENGTH:
        return SuggestResponse(query=q or "", results=[])

    owned_project_ids_stmt = select(Project.id).where(Project.owner_id == current_user.id)
    if project_id is not None:
        owned = db.execute(
            select(Project.id).where(Project.id == project_id, Project.owner_id == current_user.id)
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project_filter = CorpusPhrase.project_id == project_id
    else:
        project_filter = or_(
            CorpusPhrase.project_id.is_(None),
            CorpusPhrase.project_id.in_(owned_project_ids_stmt),
        )

    similarity = func.similarity(CorpusPhrase.normalized_phrase, normalized_prefix).label("similarity")
    candidate_stmt = (
        select(
            CorpusPhrase.phrase_text,
            CorpusPhrase.normalized_phrase,
            CorpusPhrase.source,
            CorpusPhrase.source_count,
            CorpusPhrase.corpus_density,
            similarity,
        )
        .where(project_filter)
        .where(
            or_(
                CorpusPhrase.normalized_phrase.like(f"{normalized_prefix}%"),
                similarity > 0.15,
            )
        )
        .order_by(similarity.desc())
        .limit(limit * 4)
    )
    rows = db.execute(candidate_stmt).all()

    best_by_phrase: dict[str, dict] = {}
    for phrase_text, normalized, source, source_count, corpus_density, trigram_similarity in rows:
        trigram_score = float(trigram_similarity or 0.0)
        if normalized.startswith(normalized_prefix):
            trigram_score = max(trigram_score, 0.85)
        density_score = float(corpus_density) if corpus_density is not None else 0.0
        source_count_score = _normalized_source_count(int(source_count or 0))
        entity_boost = 1.0 if source == ENTITY_SOURCE else 0.0
        hybrid_score = (
            (0.4 * trigram_score)
            + (0.3 * density_score)
            + (0.2 * source_count_score)
            + (0.1 * entity_boost)
        )

        existing = best_by_phrase.get(normalized)
        if existing is None or hybrid_score > existing["score"]:
            best_by_phrase[normalized] = {
                "phrase_text": phrase_text,
                "source": source,
                "score": round(hybrid_score, 4),
            }

    ranked = sorted(best_by_phrase.values(), key=lambda item: item["score"], reverse=True)[:limit]
    return SuggestResponse(
        query=q or "",
        results=[SuggestItem(**item) for item in ranked],
    )


@router.post("/corpus-phrases/rebuild", response_model=CorpusPhraseRebuildResponse)
def rebuild_corpus_phrases(
    payload: CorpusPhraseRebuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CorpusPhraseRebuildResponse:
    project = db.execute(
        select(Project).where(Project.id == payload.project_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    job_id = enqueue_corpus_phrase_build(project.id)
    return CorpusPhraseRebuildResponse(
        enqueued=job_id is not None,
        job_id=job_id,
        project_id=project.id,
    )
