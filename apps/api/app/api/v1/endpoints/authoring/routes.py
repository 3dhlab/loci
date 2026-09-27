"""Authenticated authoring endpoints (Stage B1 read + Stage B2 reviewer writes).

All routes require ``get_current_user`` (via ``require_role``) plus a
project-scoped RBAC role. Read routes allow any member; write routes (B2) are
limited to reviewer/approver/PM and are PROPOSAL-ONLY — they never change
approval status, run readiness snapshots, grant overrides, or touch import.
Private OCR/provenance fields are returned only inside ``private`` blocks, and
every successful mutation writes exactly one audit event in the same
transaction.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os as _os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.authoring.audit import SecretLeakError, write_audit_event
from app.api.v1.endpoints.authoring.gating import compute_gates, compute_review_state
from app.api.v1.endpoints.authoring.import_execution import (
    capture_preimport_backup,
    count_target_source_version_rows,
    perform_guarded_import,
    perform_scoped_rollback,
    resolve_target_video,
)
from app.api.v1.endpoints.authoring.imports import (
    PathConfinementError,
    build_preflight_checks,
    compute_sha256,
    confine_import_path,
    preflight_passed,
    production_imports_enabled,
    rollback_scope,
    sha256_bytes,
)
from app.services.title_card_ocr import load_title_card_import_batch_from_bytes
from app.api.v1.endpoints.authoring.rbac import (
    AUDIT_READ_ROLES,
    MEMBER_ROLES,
    ROLES,
    AuthorizationContext,
    auditor_exclusivity_violation,
    batch_project_key,
    find_project_role,
    granted_project_keys,
    granted_roles_for_project,
    grant_project_role,
    lock_role_scope,
    require_platform_admin,
    require_role,
    revoke_project_role,
)
from app.api.v1.endpoints.authoring.schemas import (
    ApprovalPrivateBlock,
    ApprovalResponse,
    AuditEventResponse,
    BatchCountsResponse,
    BatchResponse,
    BulkObservationRequest,
    BulkObservationResultResponse,
    ObservationPatchRequest,
    ObservationPrivateBlock,
    ObservationResponse,
    EvidenceOverridePrivateBlock,
    EvidenceOverrideResponse,
    ImportRequest,
    ImportRunResponse,
    ObjectLabelRequest,
    ObjectLabelResponse,
    OverrideGrantRequest,
    OverrideRejectRequest,
    PreflightCheckResponse,
    PreflightRequest,
    PreflightRunResponse,
    ProjectRoleGrantRequest,
    ProjectRoleResponse,
    ProjectRoleRevokeRequest,
    ReadinessCheckResponse,
    ReadinessResponse,
    ReadinessSnapshotRequest,
    ReadinessSnapshotResponse,
    RollbackResponse,
    SegmentApprovalBlock,
    SegmentApprovalPrivateBlock,
    SegmentApproveRequest,
    SegmentCreateRequest,
    SegmentPatchRequest,
    SegmentPrivateBlock,
    SegmentRejectRequest,
    SegmentResponse,
    SpellingConfirmRequest,
    SpellingFlagPrivateBlock,
    SpellingFlagResponse,
)
from app.db.session import get_db
from app.models.authoring_lane import (
    ApprovalDecision,
    EvidenceOverride,
    ImportRun,
    ImportRunCheck,
    ObservationReview,
    OcrBatch,
    OcrBatchFrame,
    ReadinessSnapshot,
    SegmentSpellingFlag,
    TitleCardSegment,
)
from app.models.entities import AuditEvent, User

router = APIRouter(prefix="/authoring", tags=["authoring"])

# Roles permitted to mutate proposal/review state (Stage B2). Uploader, operator,
# and auditor are intentionally excluded.
WRITE_ROLES = frozenset({"reviewer", "approver", "pm"})
# Roles permitted to approve / confirm / grant / snapshot (Stage B3).
APPROVE_ROLES = frozenset({"approver", "pm"})
# Object-label public decision is PM-only.
PM_ROLES = frozenset({"pm"})
# Stage B4 — guarded import tier. Preflight may be run by approver/PM/operator
# (and read by auditor); import execute and rollback are operator-only.
PREFLIGHT_RUN_ROLES = frozenset({"approver", "pm", "operator"})
OPERATOR_ROLES = frozenset({"operator"})

# Media files are confined to this root for execution-time SHA re-verification.
_MEDIA_ROOT = _os.environ.get("AUTHORING_MEDIA_ROOT", "/var/lib/semantic/media")


def _utc_stamp() -> str:
    """UTC timestamp (YYYYMMDDTHHMMSSZ) for backup filenames."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_batch(db: Session, batch_id: uuid.UUID) -> OcrBatch:
    batch = db.get(OcrBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    return batch


def _count(db: Session, model, *where) -> int:
    return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def _grouped_counts(db: Session, model, batch_ids: list[uuid.UUID], *where) -> dict[uuid.UUID, int]:
    """count(*) grouped by batch_id for ``batch_ids`` (optional extra filters)."""
    if not batch_ids:
        return {}
    stmt = (
        select(model.batch_id, func.count())
        .where(model.batch_id.in_(batch_ids), *where)
        .group_by(model.batch_id)
    )
    return {bid: n for bid, n in db.execute(stmt).all()}


def _bulk_batch_counts(db: Session, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, BatchCountsResponse]:
    """Counts for many batches in 5 grouped queries instead of 5x per batch."""
    frames = _grouped_counts(db, OcrBatchFrame, batch_ids)
    observations = _grouped_counts(db, ObservationReview, batch_ids)
    segments = _grouped_counts(db, TitleCardSegment, batch_ids)
    approved = _grouped_counts(db, ApprovalDecision, batch_ids, ApprovalDecision.approval_status == "approved")
    suppressed = _grouped_counts(db, ObservationReview, batch_ids, ObservationReview.is_suppressed.is_(True))
    return {
        bid: BatchCountsResponse(
            frames=frames.get(bid, 0),
            observations=observations.get(bid, 0),
            segments=segments.get(bid, 0),
            approved_segments=approved.get(bid, 0),
            suppressed_observations=suppressed.get(bid, 0),
        )
        for bid in batch_ids
    }


def _batch_response_with_counts(batch: OcrBatch, counts: BatchCountsResponse) -> BatchResponse:
    return BatchResponse(
        id=batch.id,
        website_object_id=batch.website_object_id,
        object_title=batch.object_title,
        stable_video_id=batch.stable_video_id,
        production_video_uuid=batch.production_video_uuid,
        project_slug=batch.project_slug,
        project_key=batch_project_key(batch),
        cadence_seconds=batch.cadence_seconds,
        expected_frame_count=batch.expected_frame_count,
        duration_ms=batch.duration_ms,
        created_on=batch.created_on,
        created_at=batch.created_at,
        counts=counts,
    )


def _batch_response(db: Session, batch: OcrBatch) -> BatchResponse:
    counts = _bulk_batch_counts(db, [batch.id])[batch.id]
    return _batch_response_with_counts(batch, counts)


def _restored_timestamps(db: Session, batch_id: uuid.UUID) -> set[int]:
    rows = db.execute(
        select(EvidenceOverride.timestamp_ms).where(EvidenceOverride.batch_id == batch_id)
    ).scalars().all()
    return set(rows)


def _observation_response(obs: ObservationReview, restored: set[int]) -> ObservationResponse:
    gates = compute_gates(obs)
    return ObservationResponse(
        timestamp_ms=obs.timestamp_ms,
        status=obs.status,
        title_card_visible=obs.title_card_visible,
        review_state=compute_review_state(obs, restored),
        date_gate=gates["date_gate"],
        speaker_gate=gates["speaker_gate"],
        will_publish_as=gates["will_publish_as"],
        session_date_text=obs.session_date_text,
        public_speaker_label=obs.public_speaker_label,
        included_in_date_only=obs.included_in_date_only,
        included_in_speaker_approved=obs.included_in_speaker_approved,
        is_suppressed=obs.is_suppressed,
        private=ObservationPrivateBlock(
            object_name_raw=obs.object_name_raw,
            presenter_name_raw=obs.presenter_name_raw,
            additional_text=obs.additional_text,
            raw_text_observed=obs.raw_text_observed,
            raw_payload_json=obs.raw_payload_json,
            confidence_object=obs.confidence_object,
            confidence_presenter=obs.confidence_presenter,
            confidence_session_date=obs.confidence_session_date,
            suppression_reason=obs.suppression_reason,
        ),
    )


def _segment_response(seg: TitleCardSegment, approval: ApprovalDecision | None) -> SegmentResponse:
    approval_block = None
    if approval is not None:
        approval_block = SegmentApprovalBlock(
            status=approval.approval_status,
            approved_public_speaker_label=approval.approved_public_speaker_label,
            approved_canonical_object_label=approval.approved_canonical_object_label,
            approved_canonical_accession=approval.approved_canonical_accession,
            date_approved=approval.date_approved,
            private=SegmentApprovalPrivateBlock(
                rationale=approval.rationale,
                reviewer_source=approval.reviewer_source,
                excluded_internal_timestamps_ms=approval.excluded_internal_timestamps_ms,
                restored_evidence_override_timestamps_ms=approval.restored_evidence_override_timestamps_ms,
            ),
        )
    return SegmentResponse(
        id=seg.id,
        ordinal=seg.ordinal,
        segment_label=seg.segment_label,
        start_ms=seg.start_ms,
        end_ms_exclusive=seg.end_ms_exclusive,
        start_clock=seg.start_clock,
        end_clock=seg.end_clock,
        session_date_text=seg.session_date_text,
        ready_row_count=seg.ready_row_count,
        approval=approval_block,
        private=SegmentPrivateBlock(raw_ocr_name=seg.raw_ocr_name),
    )


def _validate_segment_window(start_ms: int, end_ms_exclusive: int, duration_ms: int | None) -> None:
    if start_ms < 0 or end_ms_exclusive < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="segment window must be non-negative")
    if end_ms_exclusive <= start_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="segment end must be greater than start")
    if duration_ms is not None and end_ms_exclusive > duration_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"segment end {end_ms_exclusive} exceeds batch duration {duration_ms}",
        )


def _segment_approval(db: Session, segment_id: uuid.UUID) -> ApprovalDecision | None:
    return db.execute(
        select(ApprovalDecision).where(ApprovalDecision.segment_id == segment_id)
    ).scalar_one_or_none()


@router.get("/batches", response_model=list[BatchResponse])
def list_batches(
    website_object_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> list[BatchResponse]:
    stmt = select(OcrBatch).order_by(OcrBatch.created_at.asc(), OcrBatch.website_object_id.asc())
    if website_object_id:
        stmt = stmt.where(OcrBatch.website_object_id == website_object_id)
    # Project-scope: only batches whose project_key the caller holds a role on.
    visible_keys = granted_project_keys(db, _ctx.user.id)
    visible = [b for b in db.execute(stmt).scalars().all() if batch_project_key(b) in visible_keys]
    # Counts in grouped queries (5 total) rather than 5 per batch.
    counts = _bulk_batch_counts(db, [b.id for b in visible])
    return [_batch_response_with_counts(b, counts[b.id]) for b in visible]


@router.get("/batches/{batch_id}", response_model=BatchResponse)
def get_batch(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> BatchResponse:
    return _batch_response(db, _load_batch(db, batch_id))


@router.get("/batches/{batch_id}/observations", response_model=list[ObservationResponse])
def list_observations(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> list[ObservationResponse]:
    _load_batch(db, batch_id)
    restored = _restored_timestamps(db, batch_id)
    rows = db.execute(
        select(ObservationReview)
        .where(ObservationReview.batch_id == batch_id)
        .order_by(ObservationReview.timestamp_ms.asc())
    ).scalars().all()
    return [_observation_response(obs, restored) for obs in rows]


@router.get("/batches/{batch_id}/segments", response_model=list[SegmentResponse])
def list_segments(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> list[SegmentResponse]:
    _load_batch(db, batch_id)
    segments = db.execute(
        select(TitleCardSegment)
        .where(TitleCardSegment.batch_id == batch_id)
        .order_by(TitleCardSegment.ordinal.asc())
    ).scalars().all()
    approvals = {
        a.segment_id: a
        for a in db.execute(select(ApprovalDecision).where(ApprovalDecision.batch_id == batch_id)).scalars().all()
    }
    return [_segment_response(seg, approvals.get(seg.id)) for seg in segments]


@router.get("/batches/{batch_id}/approvals", response_model=list[ApprovalResponse])
def list_approvals(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> list[ApprovalResponse]:
    _load_batch(db, batch_id)
    segments = {s.id: s for s in db.execute(select(TitleCardSegment).where(TitleCardSegment.batch_id == batch_id)).scalars().all()}
    approvals = db.execute(select(ApprovalDecision).where(ApprovalDecision.batch_id == batch_id)).scalars().all()
    out: list[ApprovalResponse] = []
    for a in approvals:
        seg = segments.get(a.segment_id)
        out.append(ApprovalResponse(
            segment_id=a.segment_id,
            segment_label=seg.segment_label if seg else "",
            approval_status=a.approval_status,
            approved_public_speaker_label=a.approved_public_speaker_label,
            approved_canonical_object_label=a.approved_canonical_object_label,
            approved_canonical_accession=a.approved_canonical_accession,
            session_date_text=a.session_date_text,
            date_approved=a.date_approved,
            private=ApprovalPrivateBlock(
                rationale=a.rationale,
                reviewer_source=a.reviewer_source,
                excluded_internal_timestamps_ms=a.excluded_internal_timestamps_ms,
                restored_evidence_override_timestamps_ms=a.restored_evidence_override_timestamps_ms,
            ),
        ))
    return out


@router.get("/batches/{batch_id}/readiness", response_model=ReadinessResponse)
def get_readiness(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*MEMBER_ROLES)),
) -> ReadinessResponse:
    batch = _load_batch(db, batch_id)
    computed = _compute_readiness(db, batch, "speaker_approved")
    return ReadinessResponse(
        batch_id=batch.id,
        ready=computed["passed"],
        checks=computed["checks"],
        summary=computed["summary"],
    )


@router.get("/audit", response_model=list[AuditEventResponse])
def list_audit(
    event_type: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*AUDIT_READ_ROLES)),
) -> list[AuditEventResponse]:
    # Project-scope in SQL: filter on actor_json->>'project_key' BEFORE the limit
    # so the caller's window can never truncate an in-scope event. (The earlier
    # fetch-wide-then-python-filter approach could silently drop in-scope rows
    # beyond the fixed window.)
    visible_keys = granted_project_keys(db, _ctx.user.id, roles=AUDIT_READ_ROLES)
    if not visible_keys:
        return []
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.actor_json["project_key"].as_string().in_(sorted(visible_keys)))
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if subject_type:
        stmt = stmt.where(AuditEvent.subject_type == subject_type)
    rows = db.execute(stmt).scalars().all()
    return [
        AuditEventResponse(
            id=e.id,
            event_type=e.event_type,
            subject_type=e.subject_type,
            subject_id=e.subject_id,
            actor=e.actor_json or {},
            payload=e.payload_json or {},
            created_at=e.created_at,
        )
        for e in rows
    ]


# --------------------------------------------------------------------------- #
# Stage B2 — reviewer/approver/PM proposal writes (audited, proposal-only).
# --------------------------------------------------------------------------- #
# Map request field names -> ObservationReview attribute names.
_OBSERVATION_FIELD_MAP = {
    "is_suppressed": "is_suppressed",
    "suppression_reason": "suppression_reason",
    "included_in_date_only": "included_in_date_only",
    "included_in_speaker_approved": "included_in_speaker_approved",
    "proposed_public_speaker_label": "public_speaker_label",
}


def _load_observation(db: Session, batch_id: uuid.UUID, timestamp_ms: int) -> ObservationReview:
    obs = db.execute(
        select(ObservationReview).where(
            ObservationReview.batch_id == batch_id,
            ObservationReview.timestamp_ms == timestamp_ms,
        )
    ).scalar_one_or_none()
    if obs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="observation timestamp not in this batch")
    return obs


def _apply_observation_fields(obs: ObservationReview, fields: dict) -> tuple[dict, dict]:
    before: dict = {}
    after: dict = {}
    for req_name, value in fields.items():
        attr = _OBSERVATION_FIELD_MAP[req_name]
        before[attr] = getattr(obs, attr)
        setattr(obs, attr, value)
        after[attr] = value
    return before, after


@router.patch("/batches/{batch_id}/observations/{timestamp_ms}", response_model=ObservationResponse)
def patch_observation(
    batch_id: uuid.UUID,
    timestamp_ms: int,
    payload: ObservationPatchRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*WRITE_ROLES)),
) -> ObservationResponse:
    _load_batch(db, batch_id)
    fields = payload.applied_fields()
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")
    obs = _load_observation(db, batch_id, timestamp_ms)
    before, after = _apply_observation_fields(obs, fields)
    try:
        write_audit_event(
            db, event_type="authoring.observation.patch", subject_type="observation_review",
            subject_id=obs.id, ctx=ctx, before=before, after=after, request=request,
        )
    except SecretLeakError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()
    db.refresh(obs)
    return _observation_response(obs, _restored_timestamps(db, batch_id))


@router.post("/batches/{batch_id}/observations/bulk", response_model=BulkObservationResultResponse)
def bulk_patch_observations(
    batch_id: uuid.UUID,
    payload: BulkObservationRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*WRITE_ROLES)),
) -> BulkObservationResultResponse:
    _load_batch(db, batch_id)
    fields = payload.patch.applied_fields()
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")

    obs_rows = db.execute(
        select(ObservationReview).where(
            ObservationReview.batch_id == batch_id,
            ObservationReview.timestamp_ms.in_(payload.timestamps_ms),
        )
    ).scalars().all()
    found = {o.timestamp_ms: o for o in obs_rows}
    missing = sorted(set(payload.timestamps_ms) - set(found))
    if missing:
        # Reject the whole request atomically — nothing is mutated.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timestamps not in batch: {missing}",
        )

    before_by_ts: dict = {}
    after_by_ts: dict = {}
    for ts in payload.timestamps_ms:
        before, after = _apply_observation_fields(found[ts], fields)
        before_by_ts[str(ts)] = before
        after_by_ts[str(ts)] = after

    try:
        write_audit_event(
            db, event_type="authoring.observation.bulk_patch", subject_type="ocr_batch",
            subject_id=batch_id, ctx=ctx,
            before={"timestamps_ms": payload.timestamps_ms, "values": before_by_ts},
            after={"patch": fields, "values": after_by_ts},
            request=request,
        )
    except SecretLeakError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()

    restored = _restored_timestamps(db, batch_id)
    refreshed = db.execute(
        select(ObservationReview)
        .where(ObservationReview.batch_id == batch_id, ObservationReview.timestamp_ms.in_(payload.timestamps_ms))
        .order_by(ObservationReview.timestamp_ms.asc())
    ).scalars().all()
    return BulkObservationResultResponse(
        updated=len(refreshed),
        timestamps_ms=sorted(payload.timestamps_ms),
        observations=[_observation_response(o, restored) for o in refreshed],
    )


@router.post("/batches/{batch_id}/segments", response_model=SegmentResponse, status_code=status.HTTP_201_CREATED)
def create_segment(
    batch_id: uuid.UUID,
    payload: SegmentCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*WRITE_ROLES)),
) -> SegmentResponse:
    batch = _load_batch(db, batch_id)
    _validate_segment_window(payload.start_ms, payload.end_ms_exclusive, batch.duration_ms)

    existing = db.execute(
        select(TitleCardSegment).where(
            TitleCardSegment.batch_id == batch_id, TitleCardSegment.segment_label == payload.segment_label
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="segment_label already exists in this batch")

    if payload.ordinal is not None:
        ordinal = payload.ordinal
    else:
        max_ordinal = db.execute(
            select(func.max(TitleCardSegment.ordinal)).where(TitleCardSegment.batch_id == batch_id)
        ).scalar_one()
        ordinal = 0 if max_ordinal is None else int(max_ordinal) + 1

    segment = TitleCardSegment(
        batch_id=batch_id,
        ordinal=ordinal,
        segment_label=payload.segment_label,
        start_ms=payload.start_ms,
        end_ms_exclusive=payload.end_ms_exclusive,
        start_clock=payload.start_clock,
        end_clock=payload.end_clock,
        raw_ocr_name=payload.raw_ocr_name,
        session_date_text=payload.session_date_text,
        ready_row_count=payload.ready_row_count,
        proposed_by=ctx.user.id,  # records the proposer for segment-approve four-eyes
    )
    db.add(segment)
    db.flush()
    try:
        write_audit_event(
            db, event_type="authoring.segment.create", subject_type="title_card_segment",
            subject_id=segment.id, ctx=ctx, before=None,
            after={"segment_label": segment.segment_label, "start_ms": segment.start_ms,
                   "end_ms_exclusive": segment.end_ms_exclusive, "ordinal": segment.ordinal,
                   "raw_ocr_name": segment.raw_ocr_name, "session_date_text": segment.session_date_text},
            request=request,
        )
    except SecretLeakError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()
    db.refresh(segment)
    return _segment_response(segment, _segment_approval(db, segment.id))


def _load_segment(db: Session, batch_id: uuid.UUID, segment_id: uuid.UUID) -> TitleCardSegment:
    seg = db.execute(
        select(TitleCardSegment).where(
            TitleCardSegment.id == segment_id, TitleCardSegment.batch_id == batch_id
        )
    ).scalar_one_or_none()
    if seg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="segment not in this batch")
    return seg


@router.patch("/batches/{batch_id}/segments/{segment_id}", response_model=SegmentResponse)
def patch_segment(
    batch_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: SegmentPatchRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*WRITE_ROLES)),
) -> SegmentResponse:
    batch = _load_batch(db, batch_id)
    fields = payload.applied_fields()
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")
    seg = _load_segment(db, batch_id, segment_id)

    # Resulting window must remain valid.
    new_start = fields.get("start_ms", seg.start_ms)
    new_end = fields.get("end_ms_exclusive", seg.end_ms_exclusive)
    if new_start is not None and new_end is not None:
        _validate_segment_window(new_start, new_end, batch.duration_ms)

    if "segment_label" in fields and fields["segment_label"] != seg.segment_label:
        clash = db.execute(
            select(TitleCardSegment).where(
                TitleCardSegment.batch_id == batch_id, TitleCardSegment.segment_label == fields["segment_label"]
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="segment_label already exists in this batch")

    before: dict = {}
    after: dict = {}
    for name, value in fields.items():
        before[name] = getattr(seg, name)
        setattr(seg, name, value)
        after[name] = value

    try:
        write_audit_event(
            db, event_type="authoring.segment.patch", subject_type="title_card_segment",
            subject_id=seg.id, ctx=ctx, before=before, after=after, request=request,
        )
    except SecretLeakError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    db.commit()
    db.refresh(seg)
    return _segment_response(seg, _segment_approval(db, seg.id))


@router.delete("/batches/{batch_id}/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_segment(
    batch_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*WRITE_ROLES)),
) -> Response:
    _load_batch(db, batch_id)
    seg = _load_segment(db, batch_id, segment_id)

    approval = _segment_approval(db, segment_id)
    if approval is not None and (approval.approval_status or "").lower() == "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot delete an approved segment (proposal-only delete)",
        )

    before = {
        "segment_label": seg.segment_label,
        "start_ms": seg.start_ms,
        "end_ms_exclusive": seg.end_ms_exclusive,
        "ordinal": seg.ordinal,
    }
    # Harmonized with the other mutation paths: a secret-scan failure rolls back
    # and returns 422 (no partial delete, no audit row).
    _audit_or_422(
        db, event_type="authoring.segment.delete", subject_type="title_card_segment",
        subject_id=seg.id, ctx=ctx, before=before, after=None, request=request,
    )
    db.delete(seg)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Stage B3 — approval + citation readiness (approver/PM, four-eyes, audited).
# --------------------------------------------------------------------------- #
def _assert_four_eyes(actor_id: uuid.UUID, other_id: uuid.UUID | None, label: str) -> None:
    """Reject when the acting identity equals the recorded counterpart identity.

    This is the *distinct-identity* check only. Whether a null counterpart is
    acceptable is a separate policy decision — security-critical call sites
    (segment approve, evidence-override grant, import execute) must first call
    :func:`_require_counterpart_known` so a missing counterpart fails closed.
    """
    if other_id is not None and other_id == actor_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"four-eyes violation: {label} (actor must differ from {label})",
        )


def _require_counterpart_known(other_id: uuid.UUID | None, label: str) -> None:
    """Fail closed when the four-eyes counterpart identity is missing.

    A null counterpart means the segregation-of-duties separation cannot be
    verified, so the security-critical mutation is refused. There is currently
    NO legacy/backfill exemption: a null counterpart always refuses. (If one is
    ever needed it must be encoded as an explicit, audited policy and tested.)
    """
    if other_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"four-eyes violation: {label} is unknown (cannot verify distinct actor)",
        )


def _get_or_create_approval(db: Session, batch_id: uuid.UUID, segment_id: uuid.UUID) -> ApprovalDecision:
    approval = _segment_approval(db, segment_id)
    if approval is None:
        approval = ApprovalDecision(batch_id=batch_id, segment_id=segment_id, approval_status="pending_review")
        db.add(approval)
        db.flush()
    return approval


def _load_spelling_flag(db: Session, batch_id: uuid.UUID, flag_id: uuid.UUID) -> SegmentSpellingFlag:
    flag = db.execute(
        select(SegmentSpellingFlag).where(
            SegmentSpellingFlag.id == flag_id, SegmentSpellingFlag.batch_id == batch_id
        )
    ).scalar_one_or_none()
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="spelling flag not in this batch")
    return flag


def _load_override(db: Session, batch_id: uuid.UUID, override_id: uuid.UUID) -> EvidenceOverride:
    override = db.execute(
        select(EvidenceOverride).where(
            EvidenceOverride.id == override_id, EvidenceOverride.batch_id == batch_id
        )
    ).scalar_one_or_none()
    if override is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence override not in this batch")
    return override


def _override_response(o: EvidenceOverride) -> EvidenceOverrideResponse:
    return EvidenceOverrideResponse(
        id=o.id, timestamp_ms=o.timestamp_ms, status=o.status, decision=o.decision,
        session_date_text=o.session_date_text, speaker_band=o.speaker_band,
        private=EvidenceOverridePrivateBlock(raw_ocr_caveat=o.raw_ocr_caveat, handling=o.handling, reviewer_source=o.reviewer_source),
    )


def _spelling_flag_response(f: SegmentSpellingFlag) -> SpellingFlagResponse:
    return SpellingFlagResponse(
        id=f.id, field=f.field, proposed_canonical=f.proposed_canonical, confirmed_canonical=f.confirmed_canonical,
        reviewer_confirmation_required=f.reviewer_confirmation_required,
        private=SpellingFlagPrivateBlock(issue=f.issue, variants_observed=f.variants_observed,
                                         variant_timestamps_ms=f.variant_timestamps_ms, note=f.note),
    )


def _audit_or_422(db: Session, **kwargs):
    try:
        return write_audit_event(db, **kwargs)
    except SecretLeakError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post(
    "/batches/{batch_id}/segments/{segment_id}/approve", response_model=SegmentResponse,
    summary="Approve a segment's public speaker label",
    description="Approver/PM action. Records the approved public speaker label for a segment. "
    "Four-eyes: the approver must differ from (and not be a null) segment proposer. Writes one audit event.",
)
def approve_segment(
    batch_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: SegmentApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> SegmentResponse:
    _load_batch(db, batch_id)
    seg = _load_segment(db, batch_id, segment_id)
    # Four-eyes: the approver must not be the segment proposer, and the proposer
    # must be known (fail closed on a null proposer).
    _require_counterpart_known(seg.proposed_by, "segment.proposed_by")
    _assert_four_eyes(ctx.user.id, seg.proposed_by, "segment.proposed_by")

    approval = _get_or_create_approval(db, batch_id, segment_id)
    before = {"approval_status": approval.approval_status, "approved_public_speaker_label": approval.approved_public_speaker_label}
    approval.approval_status = "approved"
    approval.approved_public_speaker_label = payload.approved_public_speaker_label
    approval.approved_canonical_object_label = payload.approved_canonical_object_label
    approval.approved_canonical_accession = payload.approved_canonical_accession
    approval.session_date_text = payload.session_date_text
    approval.restored_evidence_override_timestamps_ms = payload.restored_evidence_override_timestamps_ms
    approval.rationale = payload.rationale
    approval.date_approved = payload.date_approved
    approval.approved_by = ctx.user.id
    after = {"approval_status": "approved", "approved_public_speaker_label": payload.approved_public_speaker_label,
             "approved_by": str(ctx.user.id), "rationale": payload.rationale,
             "approved_canonical_object_label": payload.approved_canonical_object_label}
    _audit_or_422(db, event_type="authoring.segment.approve", subject_type="title_card_segment",
                  subject_id=seg.id, ctx=ctx, before=before, after=after, request=request)
    db.commit()
    return _segment_response(seg, _segment_approval(db, seg.id))


@router.post("/batches/{batch_id}/segments/{segment_id}/reject", response_model=SegmentResponse)
def reject_segment(
    batch_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: SegmentRejectRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> SegmentResponse:
    _load_batch(db, batch_id)
    seg = _load_segment(db, batch_id, segment_id)
    approval = _get_or_create_approval(db, batch_id, segment_id)
    before = {"approval_status": approval.approval_status}
    approval.approval_status = "rejected"
    approval.rationale = payload.reason
    approval.approved_by = ctx.user.id
    approval.approved_public_speaker_label = None
    _audit_or_422(db, event_type="authoring.segment.reject", subject_type="title_card_segment",
                  subject_id=seg.id, ctx=ctx, before=before, after={"approval_status": "rejected", "reason": payload.reason},
                  request=request)
    db.commit()
    return _segment_response(seg, _segment_approval(db, seg.id))


@router.post("/batches/{batch_id}/spelling-flags/{flag_id}/confirm", response_model=SpellingFlagResponse)
def confirm_spelling(
    batch_id: uuid.UUID,
    flag_id: uuid.UUID,
    payload: SpellingConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> SpellingFlagResponse:
    _load_batch(db, batch_id)
    flag = _load_spelling_flag(db, batch_id, flag_id)
    before = {"confirmed_canonical": flag.confirmed_canonical}
    flag.confirmed_canonical = payload.confirmed_canonical
    flag.confirmed_by = ctx.user.id
    if payload.note is not None:
        flag.note = payload.note
    _audit_or_422(db, event_type="authoring.spelling_flag.confirm", subject_type="segment_spelling_flag",
                  subject_id=flag.id, ctx=ctx, before=before,
                  after={"confirmed_canonical": payload.confirmed_canonical, "confirmed_by": str(ctx.user.id)},
                  request=request)
    db.commit()
    db.refresh(flag)
    return _spelling_flag_response(flag)


@router.post(
    "/batches/{batch_id}/evidence-overrides/{override_id}/grant", response_model=EvidenceOverrideResponse,
    summary="Grant an evidence override (restore a suppressed timestamp)",
    description="Approver/PM action. Restores a suppressed caption-only timestamp into its surrounding "
    "speaker band. Four-eyes: grantor must differ from (and not be a null) override proposer. "
    "The private caption is never exposed publicly. Writes one audit event.",
)
def grant_override(
    batch_id: uuid.UUID,
    override_id: uuid.UUID,
    payload: OverrideGrantRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> EvidenceOverrideResponse:
    _load_batch(db, batch_id)
    override = _load_override(db, batch_id, override_id)
    # Four-eyes: the grantor must not be the override proposer, and the proposer
    # must be known (fail closed on a null proposer).
    _require_counterpart_known(override.proposed_by, "evidence_override.proposed_by")
    _assert_four_eyes(ctx.user.id, override.proposed_by, "evidence_override.proposed_by")
    before = {"status": override.status, "granted_by": str(override.granted_by) if override.granted_by else None}
    override.status = "granted"
    override.granted_by = ctx.user.id
    if payload.decision is not None:
        override.decision = payload.decision
    if payload.handling is not None:
        override.handling = payload.handling
    if payload.session_date_text is not None:
        override.session_date_text = payload.session_date_text
    if payload.speaker_band is not None:
        override.speaker_band = payload.speaker_band
    _audit_or_422(db, event_type="authoring.evidence_override.grant", subject_type="evidence_override",
                  subject_id=override.id, ctx=ctx, before=before,
                  after={"status": "granted", "granted_by": str(ctx.user.id), "decision": override.decision},
                  request=request)
    db.commit()
    db.refresh(override)
    return _override_response(override)


@router.post("/batches/{batch_id}/evidence-overrides/{override_id}/reject", response_model=EvidenceOverrideResponse)
def reject_override(
    batch_id: uuid.UUID,
    override_id: uuid.UUID,
    payload: OverrideRejectRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> EvidenceOverrideResponse:
    _load_batch(db, batch_id)
    override = _load_override(db, batch_id, override_id)
    before = {"status": override.status}
    override.status = "rejected"
    override.granted_by = ctx.user.id
    override.handling = payload.reason
    _audit_or_422(db, event_type="authoring.evidence_override.reject", subject_type="evidence_override",
                  subject_id=override.id, ctx=ctx, before=before,
                  after={"status": "rejected", "reason": payload.reason}, request=request)
    db.commit()
    db.refresh(override)
    return _override_response(override)


@router.post(
    "/batches/{batch_id}/object-label", response_model=ObjectLabelResponse,
    summary="Decide the public object label (PM only)",
    description="PM-only action. Records the approved object label + accession as private title-card "
    "metadata. This does not change the public citation payload or the public page title. Writes one audit event.",
)
def decide_object_label(
    batch_id: uuid.UUID,
    payload: ObjectLabelRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*PM_ROLES)),
) -> ObjectLabelResponse:
    batch = _load_batch(db, batch_id)
    before = {"approved_object_label": batch.approved_object_label, "approved_object_accession": batch.approved_object_accession}
    batch.approved_object_label = payload.approved_object_label
    batch.approved_object_accession = payload.approved_object_accession
    batch.object_label_decided_by = ctx.user.id
    _audit_or_422(db, event_type="authoring.object_label.decide", subject_type="ocr_batch",
                  subject_id=batch.id, ctx=ctx, before=before,
                  after={"approved_object_label": payload.approved_object_label,
                         "approved_object_accession": payload.approved_object_accession,
                         "decided_by": str(ctx.user.id)},
                  request=request)
    db.commit()
    db.refresh(batch)
    return ObjectLabelResponse(batch_id=batch.id, approved_object_label=batch.approved_object_label,
                               approved_object_accession=batch.approved_object_accession)


def _compute_readiness(db: Session, batch: OcrBatch, phase: str) -> dict:
    """Compute the readiness checklist + content digest for a batch/phase."""
    segments = db.execute(
        select(TitleCardSegment).where(TitleCardSegment.batch_id == batch.id).order_by(TitleCardSegment.ordinal.asc())
    ).scalars().all()
    approvals = {a.segment_id: a for a in db.execute(select(ApprovalDecision).where(ApprovalDecision.batch_id == batch.id)).scalars().all()}
    flags = db.execute(select(SegmentSpellingFlag).where(SegmentSpellingFlag.batch_id == batch.id)).scalars().all()
    overrides = db.execute(select(EvidenceOverride).where(EvidenceOverride.batch_id == batch.id)).scalars().all()
    observations = db.execute(select(ObservationReview).where(ObservationReview.batch_id == batch.id)).scalars().all()

    pending_segments = [s.segment_label for s in segments
                        if not (approvals.get(s.id) and approvals[s.id].approval_status in ("approved", "rejected"))]
    pending_flags = [str(f.id) for f in flags if f.reviewer_confirmation_required and not f.confirmed_canonical]
    unresolved_overrides = [str(o.id) for o in overrides if (o.status or "proposed") == "proposed"]

    include_attr = "included_in_speaker_approved" if phase == "speaker_approved" else "included_in_date_only"
    included_observations = [o for o in observations if getattr(o, include_attr)]
    row_count = len(included_observations)
    will_publish = {"named": 0, "dated_speakerless": 0, "unavailable": 0}
    for obs in observations:
        will_publish[compute_gates(obs)["will_publish_as"]] += 1

    # Public/private boundary preview: every included row's projection must only
    # surface the allowed public keys.
    allowed_public_keys = {"speaker_label", "session_date_text", "session_date_precision", "attribution_mode"}
    boundary_clean = True
    for obs in included_observations:
        preview = compute_gates(obs)["public_preview"]
        if not set(preview.keys()) <= allowed_public_keys:
            boundary_clean = False
            break

    approved_segments = [s for s in segments if approvals.get(s.id) and approvals[s.id].approval_status == "approved"]

    checks = [
        ReadinessCheckResponse(name="all_segments_resolved",
                               status="pass" if segments and not pending_segments else "fail",
                               detail=("all resolved" if (segments and not pending_segments)
                                       else (f"pending: {pending_segments}" if segments else "no segments"))),
        ReadinessCheckResponse(name="no_pending_spelling_flags",
                               status="pass" if not pending_flags else "fail",
                               detail=("none pending" if not pending_flags else f"pending: {pending_flags}")),
        ReadinessCheckResponse(name="evidence_overrides_resolved",
                               status="pass" if not unresolved_overrides else "fail",
                               detail=("all resolved" if not unresolved_overrides else f"unresolved: {unresolved_overrides}")),
        ReadinessCheckResponse(name="object_label_decided",
                               status="pass" if batch.approved_object_label else "fail",
                               detail=(batch.approved_object_label or "object-label decision required")),
        ReadinessCheckResponse(name="public_private_boundary_clean",
                               status="pass" if boundary_clean else "fail",
                               detail="public preview exposes only allowed citation keys"),
        ReadinessCheckResponse(name="expected_row_count",
                               status="pass" if row_count > 0 else "fail",
                               detail=f"{row_count} rows would be imported ({phase})"),
    ]
    passed = all(c.status == "pass" for c in checks)

    # Deterministic content digest over the approved bundle.
    digest_doc = {
        "phase": phase,
        "object_label": batch.approved_object_label,
        "object_accession": batch.approved_object_accession,
        "segments": sorted(
            [{"label": s.segment_label,
              "approved_label": approvals[s.id].approved_public_speaker_label if approvals.get(s.id) else None,
              "status": approvals[s.id].approval_status if approvals.get(s.id) else None,
              "session_date_text": approvals[s.id].session_date_text if approvals.get(s.id) else None}
             for s in segments],
            key=lambda d: d["label"],
        ),
        "included_timestamps": sorted(o.timestamp_ms for o in included_observations),
    }
    content_digest = hashlib.sha256(
        json.dumps(digest_doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return {
        "checks": checks,
        "passed": passed,
        "row_count": row_count,
        "content_digest": content_digest,
        "approved_segment_proposers": [s.proposed_by for s in approved_segments],
        "approved_segment_count": len(approved_segments),
        "summary": {"segments": len(segments), "approved_segments": len(approved_segments),
                    "observations": len(observations), "row_count": row_count,
                    "phase": phase, "will_publish_as": will_publish},
    }


@router.post(
    "/batches/{batch_id}/readiness/snapshot", response_model=ReadinessSnapshotResponse,
    summary="Freeze a citation-readiness snapshot for a source_version",
    description="Approver/PM action. Runs the readiness checklist and, if all checks pass, binds a "
    "source_version to an immutable content digest (and optional fixture/media SHAs). Reusing a "
    "source_version with different content is rejected. This snapshot is what a later import is checked against.",
)
def create_readiness_snapshot(
    batch_id: uuid.UUID,
    payload: ReadinessSnapshotRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*APPROVE_ROLES)),
) -> ReadinessSnapshotResponse:
    batch = _load_batch(db, batch_id)
    computed = _compute_readiness(db, batch, payload.phase)

    if not computed["passed"]:
        # Failed validation: no snapshot row, no audit event.
        failing = [c.model_dump() for c in computed["checks"] if c.status != "pass"]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "readiness checks failed", "checks": failing},
        )

    # Four-eyes: the snapshot author must not be the sole proposer of every
    # included (approved) segment.
    proposers = [p for p in computed["approved_segment_proposers"] if p is not None]
    if proposers and all(p == ctx.user.id for p in proposers):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="four-eyes violation: snapshot author is the sole proposer of every included segment",
        )

    # source_version immutability: same source_version must bind one content digest.
    existing = db.execute(
        select(ReadinessSnapshot).where(
            ReadinessSnapshot.batch_id == batch_id,
            ReadinessSnapshot.source_version == payload.source_version,
        )
    ).scalar_one_or_none()
    if existing is not None and existing.content_digest != computed["content_digest"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="source_version already bound to a different content digest (immutable)",
        )
    if existing is not None:
        # Idempotent re-snapshot of identical content.
        return ReadinessSnapshotResponse(
            id=existing.id, batch_id=batch_id, source_version=existing.source_version, phase=existing.phase,
            content_digest=existing.content_digest, fixture_sha256=existing.fixture_sha256,
            media_sha256=existing.media_sha256, row_count=existing.row_count, created_by=existing.created_by,
            checks=computed["checks"], created_at=existing.created_at,
        )

    snapshot = ReadinessSnapshot(
        batch_id=batch_id, source_version=payload.source_version, phase=payload.phase,
        content_digest=computed["content_digest"], fixture_sha256=payload.fixture_sha256,
        media_sha256=payload.media_sha256, row_count=computed["row_count"], created_by=ctx.user.id,
        checklist_json=[c.model_dump() for c in computed["checks"]], summary_json=computed["summary"],
    )
    db.add(snapshot)
    db.flush()
    _audit_or_422(db, event_type="authoring.readiness.snapshot", subject_type="ocr_batch",
                  subject_id=batch_id, ctx=ctx, before=None,
                  after={"source_version": payload.source_version, "phase": payload.phase,
                         "content_digest": computed["content_digest"], "row_count": computed["row_count"]},
                  request=request)
    db.commit()
    db.refresh(snapshot)
    return ReadinessSnapshotResponse(
        id=snapshot.id, batch_id=batch_id, source_version=snapshot.source_version, phase=snapshot.phase,
        content_digest=snapshot.content_digest, fixture_sha256=snapshot.fixture_sha256,
        media_sha256=snapshot.media_sha256, row_count=snapshot.row_count, created_by=snapshot.created_by,
        checks=computed["checks"], created_at=snapshot.created_at,
    )


# --------------------------------------------------------------------------- #
# Stage B4 — guarded preflight / import / rollback.
#
# These are the production-touching tier, but production execution is gated
# behind an explicit env flag + the operator role. Until then every run is a
# dry-run that records its checklist and writes no production data. No route
# here calls OCR/OpenAI, performs SSH/deploy/sync, or widens the public payload.
# --------------------------------------------------------------------------- #
def _project_batches(db: Session, project_key: str) -> list[OcrBatch]:
    """All batches sharing a project scope key (project_slug or stable_video_id)."""
    return [b for b in db.execute(select(OcrBatch)).scalars().all() if batch_project_key(b) == project_key]


def _project_reserved_source_versions(db: Session, project_key: str, exclude_batch_id: uuid.UUID) -> set[str]:
    """source_versions already imported (not rolled back) by OTHER batches in the project."""
    batch_ids = [b.id for b in _project_batches(db, project_key) if b.id != exclude_batch_id]
    if not batch_ids:
        return set()
    runs = db.execute(
        select(ImportRun).where(
            ImportRun.batch_id.in_(batch_ids),
            ImportRun.kind == "import",
            ImportRun.rolled_back_at.is_(None),
        )
    ).scalars().all()
    return {r.source_version for r in runs}


def _target_sv_rows(db: Session, batch_id: uuid.UUID, source_version: str) -> int:
    """Rows already imported for this batch+source_version (non-rolled-back)."""
    runs = db.execute(
        select(ImportRun).where(
            ImportRun.batch_id == batch_id,
            ImportRun.kind == "import",
            ImportRun.source_version == source_version,
            ImportRun.rolled_back_at.is_(None),
        )
    ).scalars().all()
    return sum(int(r.observations_written or 0) for r in runs)


def _preflight_run_response(run: ImportRun, checks: list[dict]) -> PreflightRunResponse:
    return PreflightRunResponse(
        id=run.id, batch_id=run.batch_id, kind=run.kind, mode=run.mode,
        source_version=run.source_version, phase=run.phase, passed=bool(run.preflight_passed),
        content_digest=run.content_digest, fixture_sha256=run.fixture_sha256, media_sha256=run.media_sha256,
        checks=[PreflightCheckResponse(**c) for c in checks], created_at=run.created_at,
    )


def _import_run_response(run: ImportRun) -> ImportRunResponse:
    return ImportRunResponse(
        id=run.id, batch_id=run.batch_id, kind=run.kind, mode=run.mode, status=run.status,
        source_version=run.source_version, phase=run.phase, preflight_run_id=run.preflight_run_id,
        readiness_snapshot_id=run.readiness_snapshot_id, content_digest=run.content_digest,
        model_run_id=run.model_run_id, inserted_count=run.inserted_count, updated_count=run.updated_count,
        removed_count=run.removed_count, observations_written=run.observations_written,
        target_sv_rows=run.target_sv_rows, backup_path=run.backup_path, backup_sha256=run.backup_sha256,
        backup_row_count=run.backup_row_count,
        approved_by=run.approved_by, executed_by=run.executed_by, rolled_back_at=run.rolled_back_at,
        rolled_back_by=run.rolled_back_by, rollback_deleted_count=run.rollback_deleted_count,
        created_at=run.created_at,
    )


@router.post(
    "/batches/{batch_id}/preflight", response_model=PreflightRunResponse,
    summary="Run import preflight (records a pass/fail checklist; no rows written)",
    description="Approver/PM/operator action. Evaluates every import gate (readiness green, digest match, "
    "source_version shape + uniqueness, target rows == 0, fixture SHA match, path confinement, public-contract, "
    "media SHA, backup) and records the result. Each blocked check explains why in plain language. "
    "A green preflight is required to authorize an import. Writes one audit event.",
)
def run_preflight(
    batch_id: uuid.UUID,
    payload: PreflightRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*PREFLIGHT_RUN_ROLES)),
) -> PreflightRunResponse:
    batch = _load_batch(db, batch_id)
    if payload.mode == "production" and not production_imports_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="production mode is not enabled in this environment")

    # Recompute readiness now and compare against the bound snapshot.
    computed = _compute_readiness(db, batch, payload.phase)
    snapshot = db.execute(
        select(ReadinessSnapshot).where(
            ReadinessSnapshot.batch_id == batch_id,
            ReadinessSnapshot.source_version == payload.source_version,
        )
    ).scalar_one_or_none()
    boundary_ok = any(c.name == "public_private_boundary_clean" and c.status == "pass" for c in computed["checks"])

    # fixture path confinement (does not require the file to exist).
    try:
        confine_import_path(payload.fixture_path)
        path_confined = True
    except PathConfinementError:
        path_confined = False

    project_key = batch_project_key(batch)
    checks = build_preflight_checks(
        snapshot_exists=snapshot is not None,
        snapshot_passed=computed["passed"],
        recomputed_digest=computed["content_digest"],
        snapshot_digest=snapshot.content_digest if snapshot else None,
        source_version=payload.source_version,
        existing_source_versions=_project_reserved_source_versions(db, project_key, batch_id),
        target_sv_rows=_target_sv_rows(db, batch_id, payload.source_version),
        fixture_sha_expected=snapshot.fixture_sha256 if snapshot else None,
        fixture_sha_actual=payload.fixture_sha256,
        media_present=bool(batch.source_media_host_path),
        media_sha_expected=batch.source_media_sha256,
        media_sha_actual=payload.media_sha256,
        fixture_path_confined=path_confined,
        backup_status="pending",
        public_contract_keys_ok=boundary_ok,
        mode=payload.mode,
    )
    passed = preflight_passed(checks)

    run = ImportRun(
        batch_id=batch_id, kind="preflight", mode=payload.mode, phase=payload.phase,
        source_version=payload.source_version,
        readiness_snapshot_id=snapshot.id if snapshot else None,
        content_digest=computed["content_digest"], fixture_sha256=payload.fixture_sha256,
        fixture_path=payload.fixture_path,
        media_sha256=payload.media_sha256, target_video_uuid=batch.production_video_uuid,
        target_sv_rows=_target_sv_rows(db, batch_id, payload.source_version),
        approved_by=snapshot.created_by if snapshot else None,
        preflight_passed=passed, status="preflight_passed" if passed else "preflight_failed",
    )
    db.add(run)
    db.flush()
    for c in checks:
        db.add(ImportRunCheck(import_run_id=run.id, check_name=c["name"],
                              check_status=c["status"], detail=c.get("detail")))
    _audit_or_422(db, event_type="authoring.import.preflight", subject_type="import_run",
                  subject_id=run.id, ctx=ctx, before=None,
                  after={"source_version": payload.source_version, "phase": payload.phase,
                         "mode": payload.mode, "passed": passed,
                         "failed_checks": [c["name"] for c in checks if c["status"] == "fail"]},
                  request=request)
    db.commit()
    db.refresh(run)
    return _preflight_run_response(run, checks)


@router.get("/batches/{batch_id}/preflight/{run_id}", response_model=PreflightRunResponse)
def get_preflight(
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*AUDIT_READ_ROLES)),
) -> PreflightRunResponse:
    _load_batch(db, batch_id)
    run = db.get(ImportRun, run_id)
    if run is None or run.batch_id != batch_id or run.kind != "preflight":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="preflight run not in this batch")
    check_rows = db.execute(
        select(ImportRunCheck).where(ImportRunCheck.import_run_id == run.id)
    ).scalars().all()
    checks = [{"name": c.check_name, "status": c.check_status, "detail": c.detail, "required": False} for c in check_rows]
    return _preflight_run_response(run, checks)


@router.post(
    "/batches/{batch_id}/imports", response_model=ImportRunResponse,
    summary="Execute a guarded import (operator only)",
    description="Operator-only action authorized by a green preflight. Re-verifies all gates server-side "
    "(four-eyes vs the snapshot approver, fixture re-hash of the bytes actually imported, media SHA, "
    "target rows == 0), captures a pre-import backup, then writes rows in 'production' mode or reports "
    "would-write counts in 'dry_run'. Fails closed on any gate; writes one audit event on success.",
)
def execute_batch_import(
    batch_id: uuid.UUID,
    payload: ImportRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*OPERATOR_ROLES)),
) -> ImportRunResponse:
    batch = _load_batch(db, batch_id)
    is_production = payload.mode == "production"
    if is_production and not production_imports_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="production mode is not enabled in this environment")

    preflight = db.get(ImportRun, payload.preflight_run_id)
    if preflight is None or preflight.batch_id != batch_id or preflight.kind != "preflight":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="preflight run not in this batch")
    if not preflight.preflight_passed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: preflight did not pass; import refused")

    snapshot = db.execute(
        select(ReadinessSnapshot).where(
            ReadinessSnapshot.batch_id == batch_id,
            ReadinessSnapshot.source_version == preflight.source_version,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: no readiness snapshot bound to this source_version")

    # Four-eyes: the operator executing must not be the approver who created the
    # green readiness snapshot, and that approver must be known (fail closed on a
    # null snapshot.created_by — an unattributable approval cannot authorize an
    # import).
    _require_counterpart_known(snapshot.created_by, "readiness_snapshot.created_by")
    _assert_four_eyes(ctx.user.id, snapshot.created_by, "readiness_snapshot.created_by")

    # --- Server-side re-verification (fail closed; no rows written on failure) ---
    # 1. Resolve the target production video row (provenance must exist).
    video = resolve_target_video(db, batch.production_video_uuid)
    if video is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: target video not found for this batch")

    # 2. Re-confine and re-hash the fixture the preflight bound; the bytes that
    #    were reviewed must equal the bytes about to be imported.
    if not preflight.fixture_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: preflight did not record a fixture path")
    try:
        confined_fixture = confine_import_path(preflight.fixture_path)
    except PathConfinementError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: fixture path escapes the import root")
    if not confined_fixture.is_file():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: staged fixture not found on server")
    # Single-read hardening: read the bytes ONCE, hash those bytes, and parse the
    # SAME bytes below — the bytes verified are exactly the bytes imported (no
    # time-of-check/time-of-use reopen gap).
    try:
        fixture_bytes = confined_fixture.read_bytes()
    except OSError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: staged fixture could not be read")
    actual_fixture_sha = sha256_bytes(fixture_bytes)
    if not snapshot.fixture_sha256 or actual_fixture_sha.lower() != snapshot.fixture_sha256.lower():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: fixture SHA-256 does not match the approved readiness snapshot")
    try:
        parsed_batch = load_title_card_import_batch_from_bytes(fixture_bytes)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: staged fixture is not a valid title-card import batch")

    # 3. Media SHA re-verification when the batch records a media file. The path
    #    is confined to the media root; a missing file fails closed in production.
    if batch.source_media_host_path:
        try:
            confined_media = confine_import_path(batch.source_media_host_path, _MEDIA_ROOT)
        except PathConfinementError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="gate failed: media path escapes the media root")
        if confined_media.is_file():
            actual_media_sha = compute_sha256(confined_media)
            if not batch.source_media_sha256 or actual_media_sha.lower() != batch.source_media_sha256.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="gate failed: media SHA-256 does not match the batch source media")
        elif is_production:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="gate failed: batch media file not found for media SHA verification")

    # 4. Real target-table check: no rows may exist for this source_version.
    target_rows = count_target_source_version_rows(db, video.id, preflight.source_version)
    if target_rows != 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"gate failed: {target_rows} existing rows for this source_version (must be 0)")

    # 5. Capture a real pre-import backup BEFORE any mutation (empty-state too).
    stamp = _utc_stamp()
    backup = capture_preimport_backup(db, video_id=video.id, stamp=stamp)

    # 6. Execute. dry_run records the would-write count without mutating; a
    #    production run writes the observation rows via the deterministic batch
    #    importer (replace-matching-source semantics).
    # The reviewed==imported guard applies to both modes: the parsed batch's
    # source_version must equal the approved one.
    if parsed_batch.source_version != preflight.source_version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="gate failed: fixture source_version does not match the approved source_version")
    if is_production:
        summary = perform_guarded_import(
            db, video=video, batch=parsed_batch,
            expected_source_version=preflight.source_version,
        )
        run_mode, run_status = "production", summary["status"]
        inserted, updated, removed, written = (
            summary["inserted"], summary["updated"], summary["removed"], summary["observations_written"],
        )
    else:
        would_write = len(parsed_batch.observations)
        run_mode, run_status = "dry_run", "dry_run_ok"
        inserted, updated, removed, written = would_write, 0, 0, would_write

    run = ImportRun(
        batch_id=batch_id, kind="import", mode=run_mode, phase=preflight.phase,
        source_version=preflight.source_version, preflight_run_id=preflight.id,
        readiness_snapshot_id=snapshot.id, content_digest=snapshot.content_digest,
        fixture_sha256=actual_fixture_sha, fixture_path=preflight.fixture_path,
        media_sha256=snapshot.media_sha256, target_video_uuid=video.id, target_sv_rows=target_rows,
        backup_path=backup["path"], backup_sha256=backup["sha256"], backup_row_count=backup["row_count"],
        approved_by=snapshot.created_by, executed_by=ctx.user.id, preflight_passed=True,
        inserted_count=inserted, updated_count=updated, removed_count=removed, observations_written=written,
        status=run_status,
    )
    db.add(run)
    db.flush()
    db.add(ImportRunCheck(import_run_id=run.id, check_name="preflight_green", check_status="pass",
                          detail=f"authorized by preflight {preflight.id}"))
    db.add(ImportRunCheck(import_run_id=run.id, check_name="fixture_sha_reverified", check_status="pass",
                          detail="server-recomputed fixture SHA matches snapshot"))
    db.add(ImportRunCheck(import_run_id=run.id, check_name="preimport_backup_captured", check_status="pass",
                          detail=f"{backup['row_count']} rows backed up"))
    db.add(ImportRunCheck(import_run_id=run.id, check_name="target_sv_rows_zero", check_status="pass",
                          detail="no existing rows for source_version"))
    _audit_or_422(db, event_type="authoring.import.execute", subject_type="import_run",
                  subject_id=run.id, ctx=ctx, before=None,
                  after={"source_version": run.source_version, "mode": run.mode, "status": run.status,
                         "inserted": run.inserted_count, "observations_written": run.observations_written,
                         "target_sv_rows": target_rows, "backup_row_count": backup["row_count"],
                         "executed_by": str(ctx.user.id),
                         "approved_by": str(snapshot.created_by) if snapshot.created_by else None},
                  request=request)
    db.commit()
    db.refresh(run)
    return _import_run_response(run)


@router.get("/batches/{batch_id}/imports", response_model=list[ImportRunResponse])
def list_imports(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*AUDIT_READ_ROLES)),
) -> list[ImportRunResponse]:
    _load_batch(db, batch_id)
    runs = db.execute(
        select(ImportRun).where(ImportRun.batch_id == batch_id, ImportRun.kind == "import")
        .order_by(ImportRun.created_at.asc())
    ).scalars().all()
    return [_import_run_response(r) for r in runs]


@router.get("/batches/{batch_id}/imports/{run_id}", response_model=ImportRunResponse)
def get_import(
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _ctx: AuthorizationContext = Depends(require_role(*AUDIT_READ_ROLES)),
) -> ImportRunResponse:
    _load_batch(db, batch_id)
    run = db.get(ImportRun, run_id)
    if run is None or run.batch_id != batch_id or run.kind != "import":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="import run not in this batch")
    return _import_run_response(run)


@router.post(
    "/imports/{run_id}/rollback", response_model=RollbackResponse,
    summary="Roll back an import (operator only; scoped delete)",
    description="Operator-only action. Deletes only the rows matching this import's exact "
    "video_id + source_version scope and records the deleted count. A dry-run import wrote nothing, "
    "so its rollback deletes nothing. Writes one audit event.",
)
def rollback_import(
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthorizationContext = Depends(require_role(*OPERATOR_ROLES)),
) -> RollbackResponse:
    # No batch_id in the path, so require_role resolved roles against any project;
    # re-check operator on THIS run's project scope.
    run = db.get(ImportRun, run_id)
    if run is None or run.kind != "import":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="import run not found")
    batch = db.get(OcrBatch, run.batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    project_key = batch_project_key(batch)
    if "operator" not in granted_roles_for_project(db, ctx.user.id, project_key):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="operator role required on this project to roll back")
    # require_role resolved this run without a batch_id in the path, so ctx had no
    # project scope. Stamp the run's actual project_key so the rollback audit
    # event is visible through scoped GET /audit for that project's oversight roles.
    ctx.project_key = project_key
    if run.rolled_back_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="import already rolled back")

    # Exact, minimal scope: video_id + source_version only.
    target_video = run.target_video_uuid or batch.production_video_uuid
    scope = rollback_scope(target_video, run.source_version)

    # Real scoped delete only for a production import that actually wrote rows;
    # a dry-run import wrote nothing, so its rollback deletes nothing.
    if run.mode == "production" and target_video is not None:
        deleted = perform_scoped_rollback(db, video_id=target_video, source_version=run.source_version)
    else:
        deleted = 0
    run.rolled_back_at = func.now()
    run.rolled_back_by = ctx.user.id
    run.rollback_deleted_count = deleted
    run.status = "rolled_back"
    db.add(ImportRunCheck(import_run_id=run.id, check_name="rollback_scope",
                          check_status="pass", detail=json.dumps(scope, sort_keys=True)))
    _audit_or_422(db, event_type="authoring.import.rollback", subject_type="import_run",
                  subject_id=run.id, ctx=ctx, before={"status": "completed"},
                  after={"status": "rolled_back", "scope": scope, "deleted": deleted,
                         "rolled_back_by": str(ctx.user.id)},
                  request=request)
    db.commit()
    db.refresh(run)
    return RollbackResponse(import_run_id=run.id, status=run.status, scope=scope,
                            rollback_deleted_count=run.rollback_deleted_count, rolled_back_by=run.rolled_back_by)


# --------------------------------------------------------------------------- #
# Admin — project role grants (platform-admin only).
#
# Replaces manual `project_role` DB seeding for local/staged hosted authoring.
# Platform admin is orthogonal to project roles: ordinary members, reviewers,
# approvers, PMs, operators, and auditors are all denied. Auditor read-only
# exclusivity is enforced in both directions. Exactly one audit event is written
# per successful grant/revoke; denied or invalid attempts write none.
# --------------------------------------------------------------------------- #
def _admin_audit_ctx(admin: User, project_key: str) -> AuthorizationContext:
    """A minimal audit context for a platform-admin action on a project scope."""
    return AuthorizationContext(admin, set(), project_key)


def _project_role_response(grant) -> ProjectRoleResponse:
    return ProjectRoleResponse(
        id=grant.id, user_id=grant.user_id, project_key=grant.project_key,
        role=grant.role, created_by=grant.created_by, created_at=grant.created_at,
    )


@router.post(
    "/admin/project-roles", response_model=ProjectRoleResponse,
    summary="Grant a project role to a user (platform admin only)",
    description="Platform-admin action. Grants one project-scoped role. Auditor is read-only and cannot "
    "co-hold any mutating role on the same project (enforced both directions). Idempotent: re-granting an "
    "existing role returns it without a second audit event. Writes exactly one audit event on a new grant.",
)
def admin_grant_project_role(
    payload: ProjectRoleGrantRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
) -> ProjectRoleResponse:
    target = db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target user not found")
    if payload.role not in ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown role")

    # Serialize concurrent grants/revokes for this (user, project) so the
    # exclusivity read-check-insert below cannot race another admin. Held until
    # commit/rollback.
    lock_role_scope(db, payload.user_id, payload.project_key)

    # Idempotent: an identical existing grant is a no-op (no second audit event).
    existing = find_project_role(db, user_id=payload.user_id, project_key=payload.project_key, role=payload.role)
    if existing is not None:
        return _project_role_response(existing)

    # Auditor exclusivity (both directions): granting auditor to a mutating
    # holder, or a mutating role to an auditor, is refused.
    current_roles = granted_roles_for_project(db, payload.user_id, payload.project_key)
    if auditor_exclusivity_violation(current_roles, payload.role):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="auditor is read-only and cannot co-hold a mutating role on the same project",
        )

    grant = grant_project_role(
        db, user_id=payload.user_id, project_key=payload.project_key, role=payload.role, created_by=admin.id,
    )
    _audit_or_422(
        db, event_type="authoring.project_role.grant", subject_type="project_role",
        subject_id=grant.id, ctx=_admin_audit_ctx(admin, payload.project_key), before=None,
        after={"actor_kind": "platform_admin", "user_id": str(payload.user_id),
               "project_key": payload.project_key, "role": payload.role},
        request=request,
    )
    db.commit()
    db.refresh(grant)
    return _project_role_response(grant)


@router.post(
    "/admin/project-roles/revoke", status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a project role from a user (platform admin only)",
    description="Platform-admin action. Removes exactly one project-scoped role grant. Returns 404 if no "
    "matching grant exists. Writes exactly one audit event on a successful revoke.",
)
def admin_revoke_project_role(
    payload: ProjectRoleRevokeRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
) -> Response:
    # Serialize against concurrent grants/revokes for this (user, project).
    lock_role_scope(db, payload.user_id, payload.project_key)
    grant = find_project_role(db, user_id=payload.user_id, project_key=payload.project_key, role=payload.role)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no matching role grant")
    grant_id = grant.id
    revoke_project_role(db, user_id=payload.user_id, project_key=payload.project_key, role=payload.role)
    _audit_or_422(
        db, event_type="authoring.project_role.revoke", subject_type="project_role",
        subject_id=grant_id, ctx=_admin_audit_ctx(admin, payload.project_key), before=None,
        after={"actor_kind": "platform_admin", "user_id": str(payload.user_id),
               "project_key": payload.project_key, "role": payload.role},
        request=request,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
