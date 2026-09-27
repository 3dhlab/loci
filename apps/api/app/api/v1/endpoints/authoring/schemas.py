"""Pydantic response schemas for the read-only authoring API (Stage B1).

Read schemas split PUBLIC/citation-bearing fields from PRIVATE fields: every
private OCR/provenance field is nested under a ``private`` object so a
serializer-level test can assert the public projection never reads them.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BatchCountsResponse(BaseModel):
    frames: int
    observations: int
    segments: int
    approved_segments: int
    suppressed_observations: int


class BatchResponse(BaseModel):
    id: uuid.UUID
    website_object_id: str
    object_title: str | None = None
    stable_video_id: str
    production_video_uuid: uuid.UUID | None = None
    project_slug: str | None = None
    project_key: str
    cadence_seconds: int
    expected_frame_count: int | None = None
    duration_ms: int | None = None
    created_on: str | None = None
    created_at: datetime | None = None
    counts: BatchCountsResponse


class ObservationPrivateBlock(BaseModel):
    object_name_raw: str | None = None
    presenter_name_raw: str | None = None
    additional_text: str | None = None
    raw_text_observed: str | None = None
    raw_payload_json: dict | None = None
    confidence_object: str | None = None
    confidence_presenter: str | None = None
    confidence_session_date: str | None = None
    suppression_reason: str | None = None


class ObservationResponse(BaseModel):
    timestamp_ms: int
    status: str
    title_card_visible: bool
    review_state: Literal["ready", "caption_only", "no_date", "failed", "restored", "suppressed", "approved"]
    date_gate: bool
    speaker_gate: bool
    will_publish_as: Literal["named", "dated_speakerless", "unavailable"]
    # citation-bearing (public only when gated)
    session_date_text: str | None = None
    public_speaker_label: str | None = None
    # included_in_date_only: row participates in the conservative date-only bundle
    # (a corroborated title-card row). included_in_speaker_approved: row
    # participates in the speaker-approved bundle that a B4 import would write —
    # this is the membership the readiness snapshot digests and the preflight
    # ``target_sv_rows`` / row-count checks count against. A reviewer toggles
    # these flags (Stage B2); the B4 import imports exactly the
    # included_in_speaker_approved set for the snapshot's phase.
    included_in_date_only: bool
    included_in_speaker_approved: bool
    is_suppressed: bool
    private: ObservationPrivateBlock


class SegmentApprovalBlock(BaseModel):
    status: str
    approved_public_speaker_label: str | None = None
    approved_canonical_object_label: str | None = None
    approved_canonical_accession: str | None = None
    date_approved: str | None = None
    private: "SegmentApprovalPrivateBlock"


class SegmentApprovalPrivateBlock(BaseModel):
    rationale: str | None = None
    reviewer_source: str | None = None
    excluded_internal_timestamps_ms: list | None = None
    restored_evidence_override_timestamps_ms: list | None = None


class SegmentResponse(BaseModel):
    id: uuid.UUID
    ordinal: int
    segment_label: str
    start_ms: int | None = None
    end_ms_exclusive: int | None = None
    start_clock: str | None = None
    end_clock: str | None = None
    session_date_text: str | None = None
    ready_row_count: int | None = None
    approval: SegmentApprovalBlock | None = None
    private: "SegmentPrivateBlock"


class SegmentPrivateBlock(BaseModel):
    raw_ocr_name: str | None = None


class ApprovalResponse(BaseModel):
    segment_id: uuid.UUID
    segment_label: str
    approval_status: str
    approved_public_speaker_label: str | None = None
    approved_canonical_object_label: str | None = None
    approved_canonical_accession: str | None = None
    session_date_text: str | None = None
    date_approved: str | None = None
    private: "ApprovalPrivateBlock"


class ApprovalPrivateBlock(BaseModel):
    rationale: str | None = None
    reviewer_source: str | None = None
    excluded_internal_timestamps_ms: list | None = None
    restored_evidence_override_timestamps_ms: list | None = None


class ReadinessCheckResponse(BaseModel):
    name: str
    status: Literal["pass", "fail", "info"]
    detail: str


class ReadinessResponse(BaseModel):
    batch_id: uuid.UUID
    ready: bool
    checks: list[ReadinessCheckResponse]
    summary: dict


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    subject_type: str
    subject_id: uuid.UUID | None = None
    actor: dict
    payload: dict
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Write request bodies (Stage B2) — proposal-only, no approval/status changes.
# --------------------------------------------------------------------------- #
MAX_BULK_TIMESTAMPS = 500


class ObservationPatchRequest(BaseModel):
    """Reviewer proposal edits to an observation. All fields optional; only
    provided fields are applied. ``proposed_public_speaker_label`` is a proposal
    only — it does not approve the row."""
    model_config = {"extra": "forbid"}

    is_suppressed: bool | None = None
    suppression_reason: str | None = Field(default=None, max_length=2000)
    included_in_date_only: bool | None = None
    included_in_speaker_approved: bool | None = None
    proposed_public_speaker_label: str | None = Field(default=None, max_length=512)

    def applied_fields(self) -> dict:
        return self.model_dump(exclude_unset=True)


class BulkObservationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    timestamps_ms: list[int] = Field(min_length=1, max_length=MAX_BULK_TIMESTAMPS)
    patch: ObservationPatchRequest

    @field_validator("timestamps_ms")
    @classmethod
    def _validate_timestamps(cls, value: list[int]) -> list[int]:
        if any(ts < 0 for ts in value):
            raise ValueError("timestamps_ms must be non-negative")
        if len(set(value)) != len(value):
            raise ValueError("timestamps_ms must be unique")
        return value


class SegmentCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    segment_label: str = Field(min_length=1, max_length=32)
    start_ms: int = Field(ge=0)
    end_ms_exclusive: int = Field(ge=1)
    ordinal: int | None = Field(default=None, ge=0)
    start_clock: str | None = Field(default=None, max_length=16)
    end_clock: str | None = Field(default=None, max_length=16)
    raw_ocr_name: str | None = Field(default=None, max_length=512)
    session_date_text: str | None = Field(default=None, max_length=128)
    ready_row_count: int | None = Field(default=None, ge=0)


class SegmentPatchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    segment_label: str | None = Field(default=None, min_length=1, max_length=32)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms_exclusive: int | None = Field(default=None, ge=1)
    ordinal: int | None = Field(default=None, ge=0)
    start_clock: str | None = Field(default=None, max_length=16)
    end_clock: str | None = Field(default=None, max_length=16)
    raw_ocr_name: str | None = Field(default=None, max_length=512)
    session_date_text: str | None = Field(default=None, max_length=128)
    ready_row_count: int | None = Field(default=None, ge=0)

    def applied_fields(self) -> dict:
        return self.model_dump(exclude_unset=True)


class BulkObservationResultResponse(BaseModel):
    updated: int
    timestamps_ms: list[int]
    observations: list[ObservationResponse]


# --------------------------------------------------------------------------- #
# Stage B3 — approval + readiness (approver/PM, four-eyes enforced).
# --------------------------------------------------------------------------- #
import re as _re

_SOURCE_VERSION_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SegmentApproveRequest(BaseModel):
    model_config = {"extra": "forbid"}

    approved_public_speaker_label: str | None = Field(default=None, max_length=512)
    session_date_text: str | None = Field(default=None, max_length=128)
    approved_canonical_object_label: str | None = Field(default=None, max_length=512)
    approved_canonical_accession: str | None = Field(default=None, max_length=64)
    restored_evidence_override_timestamps_ms: list[int] | None = None
    rationale: str | None = Field(default=None, max_length=2000)
    date_approved: str | None = Field(default=None, max_length=32)


class SegmentRejectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=1, max_length=2000)


class SpellingConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    confirmed_canonical: str = Field(min_length=1, max_length=512)
    note: str | None = Field(default=None, max_length=2000)


class OverrideGrantRequest(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str | None = Field(default=None, max_length=64)
    handling: str | None = Field(default=None, max_length=2000)
    session_date_text: str | None = Field(default=None, max_length=128)
    speaker_band: str | None = Field(default=None, max_length=512)


class OverrideRejectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=1, max_length=2000)


class EvidenceOverrideResponse(BaseModel):
    id: uuid.UUID
    timestamp_ms: int
    status: str
    decision: str | None = None
    session_date_text: str | None = None
    speaker_band: str | None = None
    private: "EvidenceOverridePrivateBlock"


class EvidenceOverridePrivateBlock(BaseModel):
    raw_ocr_caveat: str | None = None
    handling: str | None = None
    reviewer_source: str | None = None


class ObjectLabelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    approved_object_label: str = Field(min_length=1, max_length=512)
    approved_object_accession: str | None = Field(default=None, max_length=64)


class ObjectLabelResponse(BaseModel):
    batch_id: uuid.UUID
    approved_object_label: str | None = None
    approved_object_accession: str | None = None


class SpellingFlagResponse(BaseModel):
    id: uuid.UUID
    field: str | None = None
    proposed_canonical: str | None = None
    confirmed_canonical: str | None = None
    reviewer_confirmation_required: bool
    private: "SpellingFlagPrivateBlock"


class SpellingFlagPrivateBlock(BaseModel):
    issue: str | None = None
    variants_observed: list | None = None
    variant_timestamps_ms: dict | None = None
    note: str | None = None


_SOURCE_VERSION_DESC = (
    "Immutable version label for this reviewed bundle, e.g. "
    "'demo-cube-5s-speaker-approved-v1'. Letters/digits then "
    "letters/digits/dot/dash/underscore. Once a snapshot binds it to content it "
    "cannot be reused for different content."
)
_FIXTURE_SHA_DESC = (
    "SHA-256 (64 hex chars) of the staged fixture file. The server recomputes "
    "this from the file on disk at import and refuses if it does not match."
)
_MEDIA_SHA_DESC = (
    "Optional SHA-256 (64 hex chars) of the source media file, re-verified "
    "server-side at import when the batch records a media path."
)


class ReadinessSnapshotRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_version: str = Field(min_length=1, max_length=128, description=_SOURCE_VERSION_DESC)
    phase: Literal["speaker_approved", "date_only"] = Field(
        default="speaker_approved",
        description="Which bundle this snapshot freezes: the named speaker-approved set or the date-only set.",
    )
    fixture_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$", description=_FIXTURE_SHA_DESC)
    media_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$", description=_MEDIA_SHA_DESC)

    @field_validator("source_version")
    @classmethod
    def _validate_source_version(cls, value: str) -> str:
        if not _SOURCE_VERSION_RE.match(value):
            raise ValueError("source_version must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        return value


class ReadinessSnapshotResponse(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    source_version: str
    phase: str
    content_digest: str = Field(
        description="Deterministic SHA-256 over the approved bundle (segments, labels, dates, "
        "included timestamps). The preflight refuses if the live content no longer hashes to this.",
    )
    fixture_sha256: str | None = None
    media_sha256: str | None = None
    row_count: int = Field(description="Number of observation rows this bundle would import.")
    created_by: uuid.UUID | None = None
    checks: list[ReadinessCheckResponse]
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Stage B4 — guarded preflight / import / rollback
# --------------------------------------------------------------------------- #
class PreflightCheckResponse(BaseModel):
    name: str
    status: Literal["pass", "fail", "info"]
    detail: str | None = None
    required: bool = False


class PreflightRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_version: str = Field(min_length=1, max_length=128, description=_SOURCE_VERSION_DESC)
    phase: Literal["speaker_approved", "date_only"] = Field(
        default="speaker_approved", description="Which bundle to preflight (speaker-approved or date-only).",
    )
    fixture_path: str = Field(
        min_length=1, max_length=1024,
        description="Path to the staged fixture, relative to the server's allowlisted import root. "
        "Must stay inside that root (no absolute paths or '..' escapes).",
    )
    fixture_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$", description=_FIXTURE_SHA_DESC)
    media_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$", description=_MEDIA_SHA_DESC)
    mode: Literal["dry_run", "production"] = Field(
        default="dry_run",
        description="'dry_run' runs all gates + backup but writes no rows; 'production' writes rows and "
        "is only allowed when production import execution is enabled on the server.",
    )

    @field_validator("source_version")
    @classmethod
    def _validate_source_version(cls, value: str) -> str:
        if not _SOURCE_VERSION_RE.match(value):
            raise ValueError("source_version must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        return value


class PreflightRunResponse(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    kind: Literal["preflight", "import"]
    mode: str
    source_version: str
    phase: str
    passed: bool
    content_digest: str | None = None
    fixture_sha256: str | None = None
    media_sha256: str | None = None
    checks: list[PreflightCheckResponse]
    created_at: datetime | None = None


class ImportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    preflight_run_id: uuid.UUID = Field(
        description="ID of the green preflight run that authorizes this import (same batch, same source_version).",
    )
    mode: Literal["dry_run", "production"] = Field(
        default="dry_run",
        description="'dry_run' re-runs all gates + backup and reports would-write counts without writing; "
        "'production' performs the real scoped row write (server must have production import enabled).",
    )


class ImportRunResponse(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    kind: Literal["preflight", "import"]
    mode: str
    status: str
    source_version: str
    phase: str
    preflight_run_id: uuid.UUID | None = None
    readiness_snapshot_id: uuid.UUID | None = None
    content_digest: str | None = Field(
        default=None, description="The approved-bundle digest this import was bound to (matches the snapshot).",
    )
    model_run_id: uuid.UUID | None = None
    inserted_count: int | None = Field(default=None, description="Rows inserted (dry-run reports the would-insert count).")
    updated_count: int | None = None
    removed_count: int | None = None
    observations_written: int | None = Field(
        default=None, description="Total observation rows written by this import (or would-write in dry-run).",
    )
    target_sv_rows: int | None = Field(
        default=None,
        description="Existing rows for this source_version in the target table at execution time. "
        "Must be 0 — a non-zero value blocks the import.",
    )
    backup_path: str | None = Field(
        default=None, description="Server-side path of the pre-import backup written before any mutation.",
    )
    backup_sha256: str | None = Field(default=None, description="SHA-256 of the pre-import backup file.")
    backup_row_count: int | None = Field(
        default=None, description="Rows captured in the pre-import backup (0 for a first/empty-state import).",
    )
    approved_by: uuid.UUID | None = None
    executed_by: uuid.UUID | None = None
    rolled_back_at: datetime | None = None
    rolled_back_by: uuid.UUID | None = None
    rollback_deleted_count: int | None = Field(
        default=None, description="Rows deleted by a rollback of this import (scoped to video_id + source_version).",
    )
    created_at: datetime | None = None


class RollbackResponse(BaseModel):
    import_run_id: uuid.UUID
    status: str
    scope: dict = Field(description="The exact rollback scope: {video_id, source_version}. No other rows are touched.")
    rollback_deleted_count: int | None = Field(
        default=None, description="Number of rows deleted, matching exactly video_id + source_version.",
    )
    rolled_back_by: uuid.UUID | None = None


# --------------------------------------------------------------------------- #
# Admin — project role grants (platform-admin only)
# --------------------------------------------------------------------------- #
ProjectRoleLiteral = Literal["uploader", "reviewer", "approver", "pm", "operator", "auditor"]


class ProjectRoleGrantRequest(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: uuid.UUID = Field(description="The account to grant the role to.")
    project_key: str = Field(
        min_length=1, max_length=255,
        description="Project scope key (the batch's project_slug, or its stable_video_id when slug is null).",
    )
    role: ProjectRoleLiteral = Field(
        description="Role to grant. 'auditor' is read-only and cannot co-hold any mutating role on the same project.",
    )


class ProjectRoleRevokeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: uuid.UUID
    project_key: str = Field(min_length=1, max_length=255)
    role: ProjectRoleLiteral


class ProjectRoleResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    project_key: str
    role: str
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None


# Resolve forward references for nested private blocks.
SegmentApprovalBlock.model_rebuild()
SegmentResponse.model_rebuild()
ApprovalResponse.model_rebuild()
EvidenceOverrideResponse.model_rebuild()
SpellingFlagResponse.model_rebuild()
