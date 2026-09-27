from __future__ import annotations

import math
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.object_models import _serialize_annotation
from app.api.v1.endpoints.search import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SUGGEST_LIMIT,
    ENTITY_SOURCE,
    MAX_SEARCH_RESULT_WINDOW,
    MAX_SUGGEST_LIMIT,
    MIN_PUBLIC_SEMANTIC_RESULT_SCORE,
    MIN_SUGGEST_PREFIX_LENGTH,
    _attach_visual_result_details,
    _apply_segment_semantic_results,
    _apply_visual_rerank,
    _apply_window_semantic_results,
    _lexical_pattern,
    _normalized_source_count,
    _resolve_media_asset,
    _visual_asset_response,
)
from app.core.config import settings
from app.core.customer_keys import resolve_customer_key
from app.core.rate_limit import SUBSCRIBE_RATE_LIMIT_POLICY, enforce_rate_limit
from app.db.session import get_db
from app.models.entities import CorpusPhrase, Object, ObjectModel, ObjectModelAnnotation, Project, Segment, Subscriber, Transcript, TranscriptSource, Video, VideoStatus, VisualWindowDescription
from app.schemas.public import (
    PublicEvidencePageResponse,
    PublicObjectModelAnnotationResponse,
    PublicObjectModelResponse,
    PublicObjectPageResponse,
    PublicObjectPreviewResponse,
    PublicObjectResponse,
    PublicProjectResponse,
    PublicSegmentResponse,
    PublicStatsResponse,
    PublicTranscriptResponse,
    PublicVideoResponse,
)
from app.schemas.search import SegmentSearchRequest, SegmentSearchResponse, SegmentSearchResult, SuggestItem, SuggestResponse
from app.schemas.subscribers import (
    AdminDigestTriggerRequest,
    AdminDigestTriggerResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriberDeleteRequest,
    UnsubscribeRequest,
    UnsubscribeResponse,
)
from app.services.notify_digest_worker import send_object_digest
from app.services.ai import AIProviderError, get_embedding_provider
from app.services.media_transcode import video_playback_path
from app.services.embed_manifest import asset_cache_control
from app.services.public_media_cache import model_cache_token, video_cache_token
from app.services.public_media_paths import resolve_public_media_file
from app.services.public_media_delivery import (
    LEGACY_MEDIA_CACHE_CONTROL,
    MediaIdentityUnavailableError,
    conditional_media_response,
)
from app.services.public_evidence import (
    build_public_evidence_page,
    PublicEvidenceConflictError,
    PublicEvidenceNotFoundError,
)
from app.services.publication_manifest import PublicationManifestBuildInput, build_publication_manifest
from app.services.object_model_variants import (
    active_delivery_profile_tokens,
    lookup_public_delivery_capability,
    lookup_public_variant_candidate,
    normalize_variant_key,
    resolve_public_model_representation,
)

router = APIRouter(prefix="/public", tags=["public"])


def _normalized_public_standfirst(obj: Object) -> str | None:
    metadata = obj.metadata_json if isinstance(obj.metadata_json, dict) else {}
    raw_value = metadata.get("public_standfirst")
    if isinstance(raw_value, str):
        trimmed = raw_value.strip()
        if trimmed:
            return trimmed

    if isinstance(obj.description, str):
        trimmed_description = obj.description.strip()
        if trimmed_description:
            return trimmed_description

    return None


def _public_object_poster_url(obj: Object, model: ObjectModel | None) -> str | None:
    website_object_id = (obj.website_object_id or "").strip()
    if not website_object_id or model is None:
        return None

    if resolve_public_media_file(model.public_poster_path) is None:
        return None

    return f"/api/public/objects/{website_object_id}/poster"


def _public_model_response(db: Session, model: ObjectModel | None) -> PublicObjectModelResponse | None:
    if model is None:
        return None
    capability = lookup_public_delivery_capability(
        db,
        model.id,
        variants_enabled=getattr(settings, "public_model_variants_enabled", False),
    )
    response = PublicObjectModelResponse.model_validate(model)
    return response.model_copy(
        update={"delivery_capabilities": capability.public_payload() if capability is not None else None}
    )


def _serialize_public_object(
    obj: Object,
    *,
    evidence_url: str | None = None,
    poster_url: str | None = None,
) -> PublicObjectResponse:
    return PublicObjectResponse(
        id=obj.id,
        project_id=obj.project_id,
        name=obj.name,
        description=obj.description,
        public_standfirst=_normalized_public_standfirst(obj),
        poster_url=poster_url,
        external_url=obj.external_url,
        evidence_url=evidence_url,
        is_published=obj.is_published,
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


def _public_projects_stmt():
    return (
        select(Project)
        .join(Object, Object.project_id == Project.id)
        .where(Object.is_published.is_(True))
        .distinct()
        .order_by(Project.name.asc())
    )


def _public_objects_stmt(project_id: UUID | None = None):
    stmt = (
        select(Object)
        .join(Project, Project.id == Object.project_id)
        .where(Object.is_published.is_(True))
        .order_by(Object.name.asc())
    )
    if project_id is not None:
        stmt = stmt.where(Object.project_id == project_id)
    return stmt


def _public_objects_page_stmt(project_id: UUID | None = None):
    """Optional configured picks followed by stable name and identifier ordering."""
    picks = [item.strip().lower() for item in settings.public_object_priority_ids.split(",") if item.strip()]
    priority = case(
        {slug: index for index, slug in enumerate(picks)},
        value=func.lower(Object.website_object_id),
        else_=len(picks),
    ) if picks else None
    stmt = (
        select(Object)
        .join(Project, Project.id == Object.project_id)
        .where(Object.is_published.is_(True))
        .order_by(*([priority.asc()] if priority is not None else []), Object.name.asc(), Object.id.asc())
    )
    if project_id is not None:
        stmt = stmt.where(Object.project_id == project_id)
    return stmt


def _public_videos_stmt(project_id: UUID | None = None, object_id: UUID | None = None):
    stmt = (
        select(Video)
        .join(Object, Object.id == Video.object_id)
        .where(
            Video.object_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Object.is_published.is_(True),
        )
        .order_by(Video.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(Video.project_id == project_id)
    if object_id is not None:
        stmt = stmt.where(Video.object_id == object_id)
    return stmt


def _published_suggest_project_ids_stmt():
    return (
        select(Project.id)
        .join(Object, Object.project_id == Project.id)
        .join(Video, Video.object_id == Object.id)
        .join(Transcript, Transcript.video_id == Video.id)
        .where(
            Object.is_published.is_(True),
            Video.object_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Transcript.is_published.is_(True),
        )
        .distinct()
    )


def _public_object_evidence_urls(db: Session, objects: list[Object]) -> dict[UUID, str | None]:
    if not objects:
        return {}

    object_ids = [obj.id for obj in objects]
    models_by_object_id = {
        model.object_id: model
        for model in db.execute(
            select(ObjectModel).where(
                ObjectModel.object_id.in_(object_ids),
                ObjectModel.is_published.is_(True),
            )
        ).scalars().all()
    }

    videos_by_object_id: dict[UUID, list[Video]] = {object_id: [] for object_id in object_ids}
    for video in db.execute(
        select(Video).where(
            Video.object_id.in_(object_ids),
            Video.object_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
        )
    ).scalars().all():
        if video.object_id is not None:
            videos_by_object_id.setdefault(video.object_id, []).append(video)

    annotations_by_object_id: dict[UUID, list[ObjectModelAnnotation]] = {object_id: [] for object_id in object_ids}
    for annotation in db.execute(
        select(ObjectModelAnnotation).where(
            ObjectModelAnnotation.object_id.in_(object_ids),
            ObjectModelAnnotation.is_published.is_(True),
        )
    ).scalars().all():
        annotations_by_object_id.setdefault(annotation.object_id, []).append(annotation)

    evidence_urls: dict[UUID, str | None] = {}
    for obj in objects:
        publication_manifest = build_publication_manifest(
            PublicationManifestBuildInput(
                object_row=obj,
                model=models_by_object_id.get(obj.id),
                videos=videos_by_object_id.get(obj.id, []),
                annotations=annotations_by_object_id.get(obj.id, []),
                clips=[],
            )
        )
        evidence_urls[obj.id] = publication_manifest.object_entry.evidence_url if publication_manifest is not None else None

    return evidence_urls


def _public_object_poster_urls(db: Session, objects: list[Object]) -> dict[UUID, str | None]:
    if not objects:
        return {}

    object_ids = [obj.id for obj in objects]
    models_by_object_id = {
        model.object_id: model
        for model in db.execute(
            select(ObjectModel).where(
                ObjectModel.object_id.in_(object_ids),
                ObjectModel.is_published.is_(True),
            )
        ).scalars().all()
    }

    return {
        obj.id: _public_object_poster_url(obj, models_by_object_id.get(obj.id))
        for obj in objects
    }


def _preferred_public_transcript(db: Session, video_id: UUID) -> Transcript | None:
    rows = db.execute(
        select(Transcript)
        .where(Transcript.video_id == video_id, Transcript.is_published.is_(True))
        .order_by(Transcript.updated_at.desc())
    ).scalars().all()
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            0 if row.source == TranscriptSource.MANUAL else 1,
            -(row.updated_at or row.created_at).timestamp(),
        ),
    )[0]


def _public_object_or_404(db: Session, object_id: UUID) -> Object:
    obj = db.execute(
        select(Object).where(Object.id == object_id, Object.is_published.is_(True))
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published object not found")
    return obj


def _public_model(db: Session, object_id: UUID) -> ObjectModel | None:
    return db.execute(
        select(ObjectModel)
        .where(ObjectModel.object_id == object_id, ObjectModel.is_published.is_(True))
    ).scalar_one_or_none()


def _public_annotations(db: Session, object_id: UUID) -> list[ObjectModelAnnotation]:
    return db.execute(
        select(ObjectModelAnnotation)
        .where(
            ObjectModelAnnotation.object_id == object_id,
            ObjectModelAnnotation.is_published.is_(True),
        )
        .order_by(ObjectModelAnnotation.created_at.desc())
    ).scalars().all()


def _public_video_url(video: Video) -> str:
    return f"/api/v1/public/videos/{video.id}/stream?{urlencode({'v': video_cache_token(video)})}"


def _validate_public_media_version(
    request: Request,
    supplied_version: str | None,
    *,
    current_version: str,
) -> bool:
    """Validate a versioned URL and identify unversioned legacy requests.

    A supplied token has one unambiguous value and must match current publication
    state. Unversioned direct links remain readable under a no-store cache policy.
    """
    version_values = request.query_params.getlist("v")
    if not version_values:
        return False
    if len(version_values) != 1 or supplied_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Public media URL is no longer current.",
            headers={"Cache-Control": "no-store"},
        )
    return True


def _public_visual_thumbnail_url(transcript_window_id: UUID) -> str:
    return f"/api/v1/public/visual-windows/{transcript_window_id}/thumbnail"


def _published_visual_window_description_or_404(db: Session, transcript_window_id: UUID) -> VisualWindowDescription:
    visual_row = db.execute(
        select(VisualWindowDescription)
        .join(Transcript, Transcript.id == VisualWindowDescription.transcript_id)
        .join(Video, Video.id == VisualWindowDescription.video_id)
        .join(Object, Object.id == Video.object_id)
        .where(
            VisualWindowDescription.transcript_window_id == transcript_window_id,
            Transcript.is_published.is_(True),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Object.is_published.is_(True),
        )
    ).scalar_one_or_none()
    if visual_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published visual thumbnail not found")
    return visual_row


def _public_model_url(db: Session, object_id: UUID, model: ObjectModel | None) -> str:
    if model is None:
        return ""
    return (
        f"/api/v1/public/objects/{object_id}/model/file?"
        f"{urlencode({'v': model_cache_token(db, model)})}"
    )


def _serialize_public_annotation(
    annotation: ObjectModelAnnotation,
    *,
    evidence_url: str | None = None,
) -> PublicObjectModelAnnotationResponse:
    payload = _serialize_annotation(annotation).model_dump()
    payload.pop("created_by", None)
    payload["evidence_url"] = evidence_url
    return PublicObjectModelAnnotationResponse.model_validate(payload)


def _public_search_result_evidence_url(
    *,
    object_public_id: str | None,
    stable_video_id: str | None,
    seek_ms: int,
) -> str | None:
    normalized_object_public_id = (object_public_id or "").strip()
    if not normalized_object_public_id:
        return None

    base_path = f"/evidence/objects/{normalized_object_public_id}"
    normalized_video_id = (stable_video_id or "").strip()
    if not normalized_video_id:
        return base_path

    query_string = urlencode(
        {
            "video": normalized_video_id,
            "t": max(0, int(seek_ms or 0)),
        }
    )
    return f"{base_path}?{query_string}"


def _enrich_public_search_results(db: Session, results: list[SegmentSearchResult]) -> list[SegmentSearchResult]:
    if not results:
        return results

    video_ids = {result.video_id for result in results}
    if not video_ids:
        return results

    metadata_rows = db.execute(
        select(Video, Object, Project)
        .join(Object, Object.id == Video.object_id)
        .join(Project, Project.id == Video.project_id)
        .where(Video.id.in_(video_ids))
    ).all()
    metadata_by_video_id = {
        video.id: {
            "object_id": obj.id,
            "object_name": obj.name,
            "object_public_id": obj.website_object_id,
            "project_name": project.name,
            "project_slug": project.website_slug,
            "stable_video_id": video.stable_video_id,
        }
        for video, obj, project in metadata_rows
    }

    enriched_results: list[SegmentSearchResult] = []
    for result in results:
        metadata = metadata_by_video_id.get(result.video_id)
        if metadata is None:
            enriched_results.append(result)
            continue

        enriched_results.append(
            result.model_copy(
                update={
                    **metadata,
                    "evidence_url": _public_search_result_evidence_url(
                        object_public_id=metadata["object_public_id"],
                        stable_video_id=metadata["stable_video_id"],
                        seek_ms=result.start_ms,
                    ),
                }
            )
        )

    return enriched_results


# ----------------------------------------------------------------------------
# Public collection statistics
#
# Both the editorial landing page (/) and the public browse page (/public)
# render their visible "open now" count by reading this endpoint, so the two
# surfaces never drift on day one. Cached server-side for 60s to absorb
# traffic spikes — the underlying cascade query reuses the existing
# _public_object_evidence_urls helper which materializes models, videos and
# annotations per object, which is moderately expensive at scale.
# ----------------------------------------------------------------------------

_STATS_CACHE_TTL_SECONDS = 60.0
_stats_cache_lock = threading.Lock()
_stats_cache: dict[str, object] = {"data": None, "timestamp": 0.0}


def _compute_public_stats(db: Session) -> PublicStatsResponse:
    """Compute open_now and in_preparation counts from the live database.

    open_now is defined identically to what /api/v1/public/objects materializes
    via _public_object_evidence_urls — an object passes when it has a
    published model, a published video in READY status, and the linked
    transcript + annotations also pass publish gates. Anything else is
    in_preparation. The same definition is used client-side at
    apps/web/src/public/PublicBrowseApp.jsx (evidenceReadyObjects), so the
    landing, /public, and the cascade-aware filter all agree.
    """
    objects = db.execute(_public_objects_stmt()).scalars().all()
    published_count = len(objects)

    if published_count == 0:
        return PublicStatsResponse(open_now_count=0, in_preparation_count=0)

    evidence_urls = _public_object_evidence_urls(db, objects)
    open_now_count = sum(1 for url in evidence_urls.values() if url)
    in_preparation_count = max(0, published_count - open_now_count)

    return PublicStatsResponse(
        open_now_count=open_now_count,
        in_preparation_count=in_preparation_count,
    )


def _get_cached_public_stats(db: Session) -> PublicStatsResponse:
    """Cached wrapper over _compute_public_stats with a 60s TTL.

    Thread-safe via a module-level lock for read+write of the cache slot. A
    transient race where two requests both miss the cache and recompute
    is acceptable — the last write wins and both responses are correct.
    """
    now = time.monotonic()
    with _stats_cache_lock:
        cached = _stats_cache["data"]
        cached_at = _stats_cache["timestamp"]
        if cached is not None and (now - cached_at) < _STATS_CACHE_TTL_SECONDS:
            return cached  # type: ignore[return-value]

    fresh = _compute_public_stats(db)
    with _stats_cache_lock:
        _stats_cache["data"] = fresh
        _stats_cache["timestamp"] = time.monotonic()
    return fresh


@router.get("/stats", response_model=PublicStatsResponse)
def get_public_stats(db: Session = Depends(get_db)) -> PublicStatsResponse:
    """Public-collection stats for the landing page and /public stats panel.

    Returns open_now_count (objects passing the full publish cascade) and
    in_preparation_count (published-at-object-level but missing one or
    more cascade gates). No authentication required. Cached server-side
    for 60s to absorb landing-page traffic without thrashing the database.
    """
    return _get_cached_public_stats(db)


@router.get("/projects", response_model=list[PublicProjectResponse])
def list_public_projects(db: Session = Depends(get_db)) -> list[Project]:
    return db.execute(_public_projects_stmt()).scalars().all()


@router.get("/objects", response_model=list[PublicObjectResponse])
def list_public_objects(project_id: UUID | None = Query(default=None), db: Session = Depends(get_db)) -> list[PublicObjectResponse]:
    objects = db.execute(_public_objects_stmt(project_id)).scalars().all()
    evidence_urls = _public_object_evidence_urls(db, objects)
    poster_urls = _public_object_poster_urls(db, objects)
    return [
        _serialize_public_object(
            obj,
            evidence_url=evidence_urls.get(obj.id),
            poster_url=poster_urls.get(obj.id),
        )
        for obj in objects
    ]


@router.get("/objects/page", response_model=PublicObjectPageResponse)
def list_public_objects_page(
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=3, ge=3, le=3),
    project_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PublicObjectPageResponse:
    """Return one bounded page of published browse objects."""
    stmt = _public_objects_page_stmt(project_id)
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = db.execute(count_stmt).scalar_one()
    total_pages = math.ceil(total / page_size) if total else 0
    effective_page = min(page, total_pages) if total_pages else 1

    objects = db.execute(
        stmt.offset((effective_page - 1) * page_size).limit(page_size)
    ).scalars().all()
    evidence_urls = _public_object_evidence_urls(db, objects)
    poster_urls = _public_object_poster_urls(db, objects)
    items = [
        _serialize_public_object(
            obj,
            evidence_url=evidence_urls.get(obj.id),
            poster_url=poster_urls.get(obj.id),
        )
        for obj in objects
    ]
    return PublicObjectPageResponse(
        items=items,
        page=effective_page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


@router.get("/videos", response_model=list[PublicVideoResponse])
def list_public_videos(
    project_id: UUID | None = Query(default=None),
    object_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Video]:
    return db.execute(_public_videos_stmt(project_id, object_id)).scalars().all()


@router.get("/search/suggest", response_model=SuggestResponse)
def suggest_public_search_phrases(
    q: str = Query("", max_length=120),
    project_id: UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_SUGGEST_LIMIT, ge=1, le=MAX_SUGGEST_LIMIT),
    db: Session = Depends(get_db),
) -> SuggestResponse:
    normalized_prefix = re.sub(r"\s+", " ", (q or "").strip().lower())
    if len(normalized_prefix) < MIN_SUGGEST_PREFIX_LENGTH:
        return SuggestResponse(query=q or "", results=[])

    published_project_ids_stmt = _published_suggest_project_ids_stmt()
    if project_id is not None:
        published = db.execute(
            select(Project.id).where(Project.id == project_id, Project.id.in_(published_project_ids_stmt))
        ).scalar_one_or_none()
        if published is None:
            raise HTTPException(status_code=404, detail="Published project not found")
        project_filter = CorpusPhrase.project_id == project_id
    else:
        project_filter = CorpusPhrase.project_id.in_(published_project_ids_stmt)

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
    return SuggestResponse(query=q or "", results=[SuggestItem(**item) for item in ranked])


@router.get("/objects/{object_id}/preview", response_model=PublicObjectPreviewResponse)
def get_public_object_preview(
    object_id: UUID,
    video_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PublicObjectPreviewResponse:
    obj = _public_object_or_404(db, object_id)
    model = _public_model(db, object_id)
    annotations = _public_annotations(db, object_id)

    candidate_videos = db.execute(_public_videos_stmt(object_id=object_id)).scalars().all()
    seen_video_ids = {video.id for video in candidate_videos}
    for annotation in annotations:
        if annotation.video_id in seen_video_ids:
            continue
        matched_video = db.execute(
            select(Video)
            .join(Object, Object.id == Video.object_id)
            .where(
                Video.id == annotation.video_id,
                Video.object_id.is_not(None),
                Video.is_published.is_(True),
                Video.status == VideoStatus.READY,
                Object.is_published.is_(True),
            )
        ).scalar_one_or_none()
        if matched_video is None:
            continue
        candidate_videos.append(matched_video)
        seen_video_ids.add(matched_video.id)

    selected_video = next((row for row in candidate_videos if row.id == video_id), None)
    if selected_video is None:
        selected_video = candidate_videos[0] if candidate_videos else None

    transcript = _preferred_public_transcript(db, selected_video.id) if selected_video else None
    segments = (
        db.execute(
            select(Segment)
            .where(Segment.transcript_id == transcript.id)
            .order_by(Segment.position.asc())
        ).scalars().all()
        if transcript is not None
        else []
    )

    publication_manifest = build_publication_manifest(
        PublicationManifestBuildInput(
            object_row=obj,
            model=model,
            videos=candidate_videos,
            annotations=annotations,
            clips=[],
        )
    )
    annotation_evidence_urls = (
        {
            annotation_id: entry.evidence_url
            for annotation_id, entry in publication_manifest.annotations_by_internal_id.items()
        }
        if publication_manifest is not None
        else {}
    )

    return PublicObjectPreviewResponse(
        object=_serialize_public_object(
            obj,
            evidence_url=publication_manifest.object_entry.evidence_url if publication_manifest is not None else None,
            poster_url=_public_object_poster_url(obj, model),
        ),
        model=_public_model_response(db, model),
        model_url=_public_model_url(db, object_id, model),
        annotations=[
            _serialize_public_annotation(annotation, evidence_url=annotation_evidence_urls.get(annotation.id))
            for annotation in annotations
        ],
        videos=[PublicVideoResponse.model_validate(video) for video in candidate_videos],
        video=PublicVideoResponse.model_validate(selected_video) if selected_video is not None else None,
        video_url=_public_video_url(selected_video) if selected_video is not None else "",
        transcript=PublicTranscriptResponse.model_validate(transcript) if transcript is not None else None,
        segments=[PublicSegmentResponse.model_validate(segment) for segment in segments],
    )


@router.get(
    "/evidence/objects/{website_object_id}",
    response_model=PublicEvidencePageResponse,
    response_model_exclude_none=True,
)
def get_public_evidence_page(
    website_object_id: str,
    clip_id: str | None = Query(default=None),
    annotation_id: str | None = Query(default=None),
    video_id: str | None = Query(default=None),
    seek_ms: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
) -> PublicEvidencePageResponse:
    try:
        return build_public_evidence_page(
            db,
            website_object_id,
            clip_id=clip_id,
            annotation_id=annotation_id,
            video_id=video_id,
            seek_ms=seek_ms,
        )
    except PublicEvidenceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PublicEvidenceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.head("/objects/{object_id}/model/file", include_in_schema=False)
@router.get("/objects/{object_id}/model/file")
def stream_public_object_model(
    request: Request,
    object_id: UUID,
    variant: str | None = Query(default=None),
    variant_required: bool = Query(default=False),
    version: str | None = Query(default=None, alias="v"),
    db: Session = Depends(get_db),
) -> Response:
    obj = _public_object_or_404(db, object_id)
    model = _public_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published object model not found")

    has_current_version = _validate_public_media_version(
        request,
        version,
        current_version=(
            model_cache_token(db, model) if "v" in request.query_params else ""
        ),
    )

    # Default-deny variant resolution. Only look up a candidate when the kill
    # switch is on and the requested key is allowlisted; the resolver then
    # enforces approval/QA/selectable/SHA/file gates. Optional requests retain
    # canonical fallback; exact-tier requests fail closed. Public JSON advertises
    # only an active profile's fixed client categories and tokens; it excludes
    # inventory, provenance, storage paths, and private QA state.
    variants_enabled = getattr(settings, "public_model_variants_enabled", False)
    active_required_keys = (
        active_delivery_profile_tokens(db, model.id)
        if variants_enabled and variant_required
        else frozenset()
    )
    normalized_variant = normalize_variant_key(variant)
    required_profile_match = normalized_variant in active_required_keys
    candidate = (
        lookup_public_variant_candidate(
            db,
            model.id,
            variant,
            website_object_id=obj.website_object_id,
        )
        if variants_enabled and (not variant_required or required_profile_match)
        else None
    )
    representation = resolve_public_model_representation(
        canonical_path=model.storage_path,
        canonical_sha256=model.sha256_checksum,
        canonical_size_bytes=model.file_size_bytes,
        requested_variant=variant,
        variant_candidate=candidate,
        variants_enabled=variants_enabled,
        # An exact-tier request is a hard client safety boundary. A disabled
        # switch, invalid/stale token, inactive profile, or unavailable artifact
        # must remain required so the resolver returns the generic 503 response
        # instead of selecting the potentially much larger canonical model.
        variant_required=variant_required,
        media_root=settings.media_root,
    )
    if has_current_version:
        # Recheck after variant lookup so a concurrent publication, revocation,
        # or kill-switch transition cannot serve a newly selected body through
        # the previously current URL. Each token query stays bounded by the
        # fixed public-variant allowlist.
        _validate_public_media_version(
            request,
            version,
            current_version=model_cache_token(db, model),
        )
    if representation.required_variant_unavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Requested model representation is temporarily unavailable.",
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        )

    path = representation.path
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published object model file is missing")

    try:
        return conditional_media_response(
            request,
            path,
            media_type=model.mime_type,
            filename=model.original_filename,
            cache_control=(
                asset_cache_control()
                if has_current_version
                else LEGACY_MEDIA_CACHE_CONTROL
            ),
            content_sha256=representation.content_sha256,
            expected_size_bytes=representation.expected_size_bytes,
            require_expected_size=True,
        )
    except MediaIdentityUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public media representation is temporarily unavailable.",
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        ) from exc


@router.head("/videos/{video_id}/stream", include_in_schema=False)
@router.get("/videos/{video_id}/stream")
def stream_public_video(
    request: Request,
    video_id: UUID,
    version: str | None = Query(default=None, alias="v"),
    db: Session = Depends(get_db),
) -> Response:
    video = db.execute(
        select(Video)
        .join(Object, Object.id == Video.object_id)
        .where(
            Video.id == video_id,
            Video.object_id.is_not(None),
            Video.is_published.is_(True),
            Video.status == VideoStatus.READY,
            Object.is_published.is_(True),
        )
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published video not found")

    has_current_version = _validate_public_media_version(
        request,
        version,
        current_version=(video_cache_token(video) if "v" in request.query_params else ""),
    )

    playback_path = video_playback_path(video)
    if playback_path is None or not playback_path.exists() or not playback_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Normalized public playback asset is unavailable for this video.",
        )

    try:
        return conditional_media_response(
            request,
            playback_path,
            media_type="video/mp4",
            filename=f"{Path(video.original_filename).stem}.normalized.mp4",
            cache_control=(
                asset_cache_control()
                if has_current_version
                else LEGACY_MEDIA_CACHE_CONTROL
            ),
            content_sha256=video.playback_sha256_checksum,
            expected_size_bytes=video.playback_file_size_bytes,
            allow_legacy_weak=True,
            require_expected_size=True,
        )
    except MediaIdentityUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public media representation is temporarily unavailable.",
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        ) from exc


@router.post("/search/segments", response_model=SegmentSearchResponse)
def search_public_segments(
    payload: SegmentSearchRequest,
    db: Session = Depends(get_db),
) -> SegmentSearchResponse:
    base_filters = [
        Video.object_id.is_not(None),
        Video.is_published.is_(True),
        Video.status == VideoStatus.READY,
        Transcript.is_published.is_(True),
        Object.is_published.is_(True),
    ]
    if payload.project_id is not None:
        base_filters.append(Video.project_id == payload.project_id)
    if payload.video_id is not None:
        base_filters.append(Video.id == payload.video_id)

    page_size = min(max(int(payload.page_size), 1), DEFAULT_PAGE_SIZE)
    requested_page = max(int(payload.page), 1)
    result_window = min(max(int(payload.limit or MAX_SEARCH_RESULT_WINDOW), 1), MAX_SEARCH_RESULT_WINDOW)

    lexical_pattern = _lexical_pattern(payload.query)
    lexical_condition = (
        Segment.text.op("~*")(lexical_pattern)
        if lexical_pattern
        else Segment.text.ilike(f"%{payload.query}%")
    )

    lexical_rows = db.execute(
        select(Segment, Transcript, Video)
        .join(Transcript, Transcript.id == Segment.transcript_id)
        .join(Video, Video.id == Transcript.video_id)
        .join(Project, Project.id == Video.project_id)
        .join(Object, Object.id == Video.object_id)
        .where(and_(*base_filters), lexical_condition)
        .order_by(Video.updated_at.desc(), Segment.start_ms.asc())
        .limit(result_window)
    ).all()

    result_map: dict[UUID, SegmentSearchResult] = {}
    for segment, transcript, video in lexical_rows:
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
            rank_score=1.0,
        )

    semantic_rows: list = []
    retrieval_mode: str | None = None
    try:
        if not settings.embedding_enabled:
            raise AIProviderError("Embedding retrieval is disabled by configuration.")
        provider = get_embedding_provider()
        embedding_result = provider.embed_texts([payload.query])
        embedding = embedding_result.vectors[0]
        retrieval_summary = _apply_window_semantic_results(
            db,
            base_filters=base_filters,
            embedding=embedding,
            result_window=result_window,
            result_map=result_map,
        )
        semantic_rows = retrieval_summary.rows
        retrieval_mode = retrieval_summary.retrieval_mode
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
        elif payload.retrieval_mode == "combined":
            _apply_visual_rerank(db, embedding=embedding, result_map=result_map)
            retrieval_mode = "combined"
    except AIProviderError:
        semantic_rows = []

    all_ranked = sorted(
        (
            item
            for item in result_map.values()
            if item.lexical_match or item.semantic_score is None or item.semantic_score >= MIN_PUBLIC_SEMANTIC_RESULT_SCORE
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
    ranked = all_ranked[(page - 1) * page_size : page * page_size]
    _attach_visual_result_details(db, ranked)
    for item in ranked:
        if item.transcript_window_id is not None and item.thumbnail_path:
            item.thumbnail_url = _public_visual_thumbnail_url(item.transcript_window_id)
    ranked = _enrich_public_search_results(db, ranked)

    return SegmentSearchResponse(
        search_log_id=None,
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
def get_public_visual_window_thumbnail(
    transcript_window_id: UUID,
    db: Session = Depends(get_db),
) -> FileResponse:
    visual_row = _published_visual_window_description_or_404(db, transcript_window_id)
    asset_path = _resolve_media_asset(visual_row.thumbnail_path)
    if asset_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published visual thumbnail not found")
    return _visual_asset_response(asset_path)


# ============================================================================
# Subscriber endpoints
#
# Four routes — all unauthenticated, all rate-limited:
#   POST /api/v1/public/subscribers              create / resubscribe
#   POST /api/v1/public/subscribers/unsubscribe  token-auth soft-delete (JSON)
#   GET  /api/v1/public/subscribers/unsubscribe  token-auth soft-delete (HTML
#                                                 confirmation page for email
#                                                 link clicks)
#   POST /api/v1/public/subscribers/delete       token-auth GDPR hard-delete
#
# Security notes:
#  - customer_key is ALWAYS derived from request Host header server-side;
#    the SubscribeRequest schema deliberately has no customer_key field.
#  - Unrecognized hosts return 400 — no silent default partition.
#  - 5/min/IP rate limit on subscribe via SUBSCRIBE_RATE_LIMIT_POLICY (in
#    addition to the middleware-applied 60/min PUBLIC_RATE_LIMIT_POLICY).
#  - Unsubscribe + delete return 200 with a descriptive status field even
#    when the token doesn't match — defeats token-enumeration timing
#    attacks where an attacker tries random tokens to learn which exist.
#  - Unsubscribe-via-link (GET) is a convenience for email recipients who
#    click the unsubscribe URL in a digest; same semantics as the POST
#    JSON variant but renders a static confirmation HTML page.
# ============================================================================


_UNSUBSCRIBE_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex, nofollow">
  <title>Loci — {title}</title>
  <style>
    :root {{ color-scheme: light; }}
    html, body {{
      margin: 0; padding: 0;
      background: #f4efe6; color: #152231;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif;
      min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
    }}
    main {{
      max-width: 480px; padding: 2rem 2.5rem;
      background: #fffdfa; border: 1px solid #d9d2c8; border-radius: 12px;
      text-align: center;
      box-shadow: 0 18px 34px rgba(48, 34, 18, 0.08);
    }}
    h1 {{
      font-family: 'Fraunces', Georgia, serif;
      font-weight: 400; font-size: 1.6rem; margin: 0 0 0.85rem;
      letter-spacing: -0.018em;
    }}
    p {{ margin: 0; color: #516070; line-height: 1.55; }}
    a {{
      color: #ca5f22; text-decoration: none; font-weight: 500;
      display: inline-block; margin-top: 1.4rem;
    }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="/public">Return to Loci</a>
  </main>
</body>
</html>"""


def _unsubscribe_page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    """Render the small confirmation page used by the GET unsubscribe-link route."""
    html = _UNSUBSCRIBE_HTML_TEMPLATE.format(title=title, message=message)
    return HTMLResponse(content=html, status_code=status_code)


def _normalized_email(value: str) -> str:
    """Canonical lowercase + trim form for storage and lookup.

    Pydantic's EmailStr already validates structure; this helper just
    normalizes for the unique (email, customer_key) lookup.
    """
    return value.strip().lower()


@router.post(
    "/subscribers",
    response_model=SubscribeResponse,
    status_code=status.HTTP_201_CREATED,
)
def subscribe_endpoint(
    request: Request,
    response: Response,
    body: SubscribeRequest,
    db: Session = Depends(get_db),
) -> SubscribeResponse:
    """Subscribe to the partner's notify-me list.

    Idempotent on (email, customer_key):
      - new pair                   → 201 Created with status=created
      - existing + active          → 200 OK with status=already_subscribed
      - existing + unsubscribed    → 200 OK with status=resubscribed
                                     (clears unsubscribed_at, keeps the
                                      original token + created_at)
    """
    enforce_rate_limit(request, SUBSCRIBE_RATE_LIMIT_POLICY)

    customer_key = resolve_customer_key(request.headers.get("host"))
    if customer_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unrecognized request origin.",
        )

    email = _normalized_email(body.email)

    existing = db.execute(
        select(Subscriber).where(
            Subscriber.email == email,
            Subscriber.customer_key == customer_key,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.unsubscribed_at is not None:
            existing.unsubscribed_at = None
            # Refresh source_url / language / user_agent_class on resubscribe
            # so we have the latest signal for future debugging.
            if body.source_url:
                existing.source_url = body.source_url
            if body.user_agent_class:
                existing.user_agent_class = body.user_agent_class
            if body.language:
                existing.language = body.language
            db.commit()
            response.status_code = status.HTTP_200_OK
            return SubscribeResponse(status="resubscribed")
        response.status_code = status.HTTP_200_OK
        return SubscribeResponse(status="already_subscribed")

    new_subscriber = Subscriber(
        id=uuid.uuid4(),
        email=email,
        customer_key=customer_key,
        unsubscribe_token=secrets.token_urlsafe(32),
        source_url=body.source_url,
        user_agent_class=body.user_agent_class,
        language=body.language,
    )
    db.add(new_subscriber)
    db.commit()
    return SubscribeResponse(status="created")


@router.post("/subscribers/unsubscribe", response_model=UnsubscribeResponse)
def unsubscribe_endpoint(
    body: UnsubscribeRequest,
    db: Session = Depends(get_db),
) -> UnsubscribeResponse:
    """Soft-delete via unsubscribed_at = now().

    Always returns 200; differentiates outcomes via status field. This
    intentionally treats "unknown token" the same as "valid token" timing-
    wise so an attacker enumerating random tokens cannot tell which exist.
    """
    sub = db.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == body.token)
    ).scalar_one_or_none()

    if sub is None:
        return UnsubscribeResponse(status="not_found")
    if sub.unsubscribed_at is not None:
        return UnsubscribeResponse(status="already_unsubscribed")

    sub.unsubscribed_at = datetime.now(timezone.utc)
    db.commit()
    return UnsubscribeResponse(status="unsubscribed")


@router.get("/subscribers/unsubscribe", response_class=HTMLResponse)
def unsubscribe_via_link(
    token: str = Query(..., min_length=20, max_length=128),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One-click email-link unsubscribe.

    Same semantics as POST /unsubscribe but returns a static HTML
    confirmation page. The token is provided as a query string so the
    user only has to click a single link in their inbox. The page is
    self-contained inline CSS — does not load the SPA bundle, does not
    leak any other subscriber data.

    `noindex, nofollow` meta tag on the rendered page so search crawlers
    that ever land here don't accidentally trigger an unsubscribe.
    """
    sub = db.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    ).scalar_one_or_none()

    if sub is None:
        return _unsubscribe_page(
            title="Link expired or invalid",
            message="This unsubscribe link is no longer valid. If you continue to "
            "receive emails from Loci, reply to any digest and we'll remove "
            "you manually.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if sub.unsubscribed_at is None:
        sub.unsubscribed_at = datetime.now(timezone.utc)
        db.commit()

    return _unsubscribe_page(
        title="You've been unsubscribed",
        message="We won't email you any more. You can subscribe again at any "
        "time from the Loci homepage.",
    )


@router.post(
    "/subscribers/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_subscriber_endpoint(
    body: SubscriberDeleteRequest,
    db: Session = Depends(get_db),
) -> Response:
    """GDPR right-to-erasure: hard-delete the subscriber row by token.

    Returns 204 unconditionally — defeats token enumeration timing
    attacks where an attacker tries random tokens to learn which match.
    A successful delete is indistinguishable on the wire from a no-op.
    """
    sub = db.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == body.token)
    ).scalar_one_or_none()

    if sub is not None:
        db.delete(sub)
        db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================
# Admin digest-trigger endpoint
#
# Operator calls this on the public stack after publish + sync to fan a
# notify-me digest email to subscribers in the calling host's partition.
#
# Authentication: shared bearer token (settings.loci_digest_trigger_token).
# The token is empty by default — the endpoint is gated off until ops
# injects a real value in .env.public. This is intentional: a misconfigured
# deploy cannot accidentally expose digest fan-out to the world.
#
# customer_key is derived from Host (same as subscribe endpoint). The
# request body cannot specify it — operator cannot accidentally fire a
# another partner’s digest from a museum-x deploy.
#
# Returns the run counts so the caller (and the operator's terminal) can
# see how the fan-out played out: total / eligible / sent / skipped /
# failed. See app.services.notify_digest_worker.DigestRunCounts.
# ============================================================================


def _check_digest_trigger_auth(request: Request) -> None:
    """Validate the shared bearer token for the admin trigger endpoint.

    Raises HTTPException(401) if:
      - the server has no token configured (gate-off-by-default), OR
      - the request supplies no Authorization header, OR
      - the supplied token doesn't match (constant-time compare).
    """
    expected = (settings.loci_digest_trigger_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Digest trigger endpoint is not configured on this deploy.",
        )

    header = request.headers.get("authorization", "").strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    supplied = header[7:].strip()

    # secrets.compare_digest is constant-time across length and content,
    # defending against timing-based credential guessing.
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid digest trigger credential.",
        )


@router.post(
    "/admin/digest/trigger",
    response_model=AdminDigestTriggerResponse,
)
def admin_trigger_digest(
    request: Request,
    body: AdminDigestTriggerRequest,
    db: Session = Depends(get_db),
) -> AdminDigestTriggerResponse:
    """Fan a notify-me digest email out to subscribers for one object.

    Operator-driven endpoint. After the local authoring console runs
    a publish + sync, the operator (or a sync-completion hook) calls
    this endpoint with the new object's slug, title, and a short
    evidence excerpt; the worker reads subscribers from the public
    stack's database, renders the LOCI-branded digest, and ships via
    Postmark (or dry-runs if the Postmark token is unset).

    Auth: Bearer {settings.loci_digest_trigger_token}.
    Idempotent within the 24h per-subscriber gate — safe to retry on
    transient network failures.
    """
    _check_digest_trigger_auth(request)

    customer_key = resolve_customer_key(request.headers.get("host"))
    if customer_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unrecognized request origin.",
        )

    base_url = (settings.semantic_embed_public_base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="semantic_embed_public_base_url is not configured.",
        )
    deep_link = f"{base_url}/evidence/objects/{body.object_slug}"

    counts = send_object_digest(
        db,
        customer_key=customer_key,
        object_title=body.object_title,
        evidence_excerpt=body.evidence_excerpt,
        deep_link=deep_link,
        base_url=base_url,
    )

    return AdminDigestTriggerResponse(
        total=counts["total"],
        eligible=counts["eligible"],
        sent=counts["sent"],
        skipped=counts["skipped"],
        failed=counts["failed"],
    )
