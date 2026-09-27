"""Private authoring / review lane models (Priority 0.5).

Additive, infrastructure-only persistence for the proven title-card
OCR -> review -> approval -> import lane. These tables capture the artifact
metadata that the six Priority 0 objects produced as committed files, so the
lane can be operated from the database instead of loose JSON/markdown.

Design notes:
- Nothing here changes the public API contract. Private OCR fields
  (``raw_text_observed``, ``additional_text``, presenter raw names, raw payloads,
  reviewer rationales, OCR caveats) live ONLY in these tables and are never
  projected into public citation payloads.
- Video linkage is intentionally by ``stable_video_id`` string plus a nullable
  ``production_video_uuid``; there is no FK to ``videos`` so the lane can be
  backfilled in a dev DB that has not seeded the production video rows.
- All foreign keys point only at lane tables, so the migration is fully
  additive and a downgrade drops only these tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ProjectRole(Base):
    """A project-scoped RBAC grant for the private authoring API.

    Scoped by ``project_key`` (the batch's project_slug when present, else its
    stable_video_id) so objects with a null project_slug are still grantable.
    Roles: uploader, reviewer, approver, pm, operator, auditor. Auditor is a
    read-only role and must not co-hold a mutating role on the same project
    (enforced by ``app.api.v1.endpoints.authoring.rbac.grant_project_role``).
    """

    __tablename__ = "project_role"
    __table_args__ = (
        UniqueConstraint("user_id", "project_key", "role", name="uq_project_role_user_project_role"),
        CheckConstraint(
            "role IN ('uploader', 'reviewer', 'approver', 'pm', 'operator', 'auditor')",
            name="ck_project_role_role_valid",
        ),
        Index("ix_project_role_user", "user_id"),
        Index("ix_project_role_project_key", "project_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_key: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OcrBatch(Base):
    """One row per object package (the title-card OCR batch for one video)."""

    __tablename__ = "ocr_batch"
    __table_args__ = (
        UniqueConstraint("website_object_id", "stable_video_id", name="uq_ocr_batch_object_video"),
        Index("ix_ocr_batch_stable_video_id", "stable_video_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    website_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    object_title: Mapped[str | None] = mapped_column(Text)
    stable_video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    production_video_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    object_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_slug: Mapped[str | None] = mapped_column(String(255))
    video_title: Mapped[str | None] = mapped_column(Text)
    source_media_host_path: Mapped[str | None] = mapped_column(Text)
    source_media_sha256: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cadence_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expected_frame_count: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(String(32))
    source_version_raw: Mapped[str | None] = mapped_column(String(128))
    source_version_date_only: Mapped[str | None] = mapped_column(String(128))
    source_version_speaker_approved: Mapped[str | None] = mapped_column(String(128))
    artifact_dir: Mapped[str] = mapped_column(Text, nullable=False)
    created_on: Mapped[str | None] = mapped_column(String(32))
    # Object-label decision (PM) — private metadata, never widens the public payload.
    approved_object_label: Mapped[str | None] = mapped_column(Text)
    approved_object_accession: Mapped[str | None] = mapped_column(String(64))
    object_label_decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    frames: Mapped[list["OcrBatchFrame"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    observations: Mapped[list["ObservationReview"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    segments: Mapped[list["TitleCardSegment"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    spelling_flags: Mapped[list["SegmentSpellingFlag"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence_overrides: Mapped[list["EvidenceOverride"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )
    import_runs: Mapped[list["ImportRun"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True
    )


class OcrBatchFrame(Base):
    """One row per sampled five-second frame timestamp in a batch."""

    __tablename__ = "ocr_batch_frame"
    __table_args__ = (
        UniqueConstraint("batch_id", "timestamp_ms", name="uq_ocr_batch_frame_batch_timestamp"),
        Index("ix_ocr_batch_frame_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_filename: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="frames")


class ObservationReview(Base):
    """One row per raw OCR observation, with private OCR text and review flags.

    ``raw_text_observed``, ``additional_text``, ``presenter_name_raw`` and
    ``raw_payload_json`` are PRIVATE and must never be projected publicly.
    """

    __tablename__ = "observation_review"
    __table_args__ = (
        UniqueConstraint("batch_id", "timestamp_ms", name="uq_observation_review_batch_timestamp"),
        Index("ix_observation_review_batch_timestamp", "batch_id", "timestamp_ms"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    title_card_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    object_name_raw: Mapped[str | None] = mapped_column(Text)
    presenter_name_raw: Mapped[str | None] = mapped_column(Text)
    session_date_text: Mapped[str | None] = mapped_column(Text)
    confidence_object: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    confidence_presenter: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    confidence_session_date: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    additional_text: Mapped[str | None] = mapped_column(Text)
    raw_text_observed: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB)
    public_speaker_label: Mapped[str | None] = mapped_column(Text)
    included_in_date_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    included_in_speaker_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suppression_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="observations")


class TitleCardSegment(Base):
    """One row per proposed presenter/identity segment (geometry + raw identity)."""

    __tablename__ = "title_card_segment"
    __table_args__ = (
        UniqueConstraint("batch_id", "segment_label", name="uq_title_card_segment_batch_label"),
        Index("ix_title_card_segment_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    segment_label: Mapped[str] = mapped_column(String(32), nullable=False)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms_exclusive: Mapped[int | None] = mapped_column(Integer)
    start_clock: Mapped[str | None] = mapped_column(String(16))
    end_clock: Mapped[str | None] = mapped_column(String(16))
    raw_ocr_name: Mapped[str | None] = mapped_column(Text)
    session_date_text: Mapped[str | None] = mapped_column(Text)
    ready_row_count: Mapped[int | None] = mapped_column(Integer)
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="segments")
    approval: Mapped["ApprovalDecision"] = relationship(
        back_populates="segment", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class ApprovalDecision(Base):
    """One PM approval decision per segment (approved label + status + scope)."""

    __tablename__ = "approval_decision"
    __table_args__ = (
        UniqueConstraint("segment_id", name="uq_approval_decision_segment"),
        Index("ix_approval_decision_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("title_card_segment.id", ondelete="CASCADE"), nullable=False
    )
    approved_public_speaker_label: Mapped[str | None] = mapped_column(Text)
    approval_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    approved_canonical_object_label: Mapped[str | None] = mapped_column(Text)
    approved_canonical_accession: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_date_text: Mapped[str | None] = mapped_column(Text)
    excluded_internal_timestamps_ms: Mapped[list | None] = mapped_column(JSONB)
    restored_evidence_override_timestamps_ms: Mapped[list | None] = mapped_column(JSONB)
    reviewer_source: Mapped[str | None] = mapped_column(String(128))
    rationale: Mapped[str | None] = mapped_column(Text)
    date_approved: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    segment: Mapped[TitleCardSegment] = relationship(back_populates="approval")


class SegmentSpellingFlag(Base):
    """A spelling / canonicalization follow-up for an object or presenter name."""

    __tablename__ = "segment_spelling_flag"
    __table_args__ = (Index("ix_segment_spelling_flag_batch", "batch_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("title_card_segment.id", ondelete="SET NULL")
    )
    field: Mapped[str | None] = mapped_column(String(128))
    issue: Mapped[str | None] = mapped_column(Text)
    variants_observed: Mapped[list | None] = mapped_column(JSONB)
    variant_timestamps_ms: Mapped[dict | None] = mapped_column(JSONB)
    proposed_canonical: Mapped[str | None] = mapped_column(Text)
    confirmed_canonical: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewer_confirmation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="spelling_flags")


class EvidenceOverride(Base):
    """A PM evidence override restoring a suppressed timestamp into a speaker band.

    ``raw_ocr_caveat`` is PRIVATE (it quotes the lower-third caption) and must
    never appear in a public citation field.
    """

    __tablename__ = "evidence_override"
    __table_args__ = (
        UniqueConstraint("batch_id", "timestamp_ms", name="uq_evidence_override_batch_timestamp"),
        Index("ix_evidence_override_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("title_card_segment.id", ondelete="SET NULL")
    )
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(64))
    approved_on: Mapped[str | None] = mapped_column(String(32))
    reviewer_source: Mapped[str | None] = mapped_column(String(128))
    session_date_text: Mapped[str | None] = mapped_column(Text)
    speaker_band: Mapped[str | None] = mapped_column(Text)
    raw_ocr_caveat: Mapped[str | None] = mapped_column(Text)
    handling: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="evidence_overrides")


class ImportRun(Base):
    """A guarded preflight or import for a batch (Stage B4) or a recorded closeout.

    ``kind`` distinguishes a non-mutating ``preflight`` run from an ``import``
    run. ``mode`` is ``dry_run`` unless production execution is explicitly
    enabled. Four-eyes provenance is captured by ``approved_by`` (the readiness
    snapshot creator) and ``executed_by`` (the operator running the import);
    the route refuses an import where these are equal. Rollback state
    (``rolled_back_*``) records a scoped ``video_id + source_version`` delete.
    """

    __tablename__ = "import_run"
    __table_args__ = (
        Index("ix_import_run_batch", "batch_id"),
        Index("ix_import_run_kind", "kind"),
        Index("ix_import_run_source_version", "source_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="import")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="dry_run")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="speaker_approved")
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    readiness_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    preflight_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    content_digest: Mapped[str | None] = mapped_column(String(64))
    fixture_sha256: Mapped[str | None] = mapped_column(String(64))
    fixture_path: Mapped[str | None] = mapped_column(Text)
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    target_video_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_sv_rows: Mapped[int | None] = mapped_column(Integer)
    backup_path: Mapped[str | None] = mapped_column(Text)
    backup_sha256: Mapped[str | None] = mapped_column(String(64))
    backup_row_count: Mapped[int | None] = mapped_column(Integer)
    executed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    preflight_passed: Mapped[bool | None] = mapped_column(Boolean)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    inserted_count: Mapped[int | None] = mapped_column(Integer)
    updated_count: Mapped[int | None] = mapped_column(Integer)
    removed_count: Mapped[int | None] = mapped_column(Integer)
    observations_written: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="recorded")
    closeout_path: Mapped[str | None] = mapped_column(Text)
    imported_on: Mapped[str | None] = mapped_column(String(32))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rollback_deleted_count: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped[OcrBatch] = relationship(back_populates="import_runs")
    checks: Mapped[list["ImportRunCheck"]] = relationship(
        back_populates="import_run", cascade="all, delete-orphan", passive_deletes=True
    )


class ImportRunCheck(Base):
    """A single QA/preflight check recorded against an import run."""

    __tablename__ = "import_run_check"
    __table_args__ = (Index("ix_import_run_check_run", "import_run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_run.id", ondelete="CASCADE"), nullable=False
    )
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    check_status: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    import_run: Mapped[ImportRun] = relationship(back_populates="checks")


class ReadinessSnapshot(Base):
    """A content-hashed citation-readiness bundle binding a ``source_version``.

    Produced by an approver/PM only when all readiness checks pass. The
    ``source_version`` is unique per batch and bound to one ``content_digest``;
    reuse of the same source_version with a different digest is rejected at the
    service layer (immutability of the reviewed-thing-equals-imported-thing).
    """

    __tablename__ = "readiness_snapshot"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_version", name="uq_readiness_snapshot_batch_source_version"),
        Index("ix_readiness_snapshot_batch", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ocr_batch.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="speaker_approved")
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_sha256: Mapped[str | None] = mapped_column(String(64))
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    checklist_json: Mapped[list | None] = mapped_column(JSONB)
    summary_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
