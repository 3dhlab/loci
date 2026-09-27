from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, Index, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.core.config import settings


def _default_annotation_relations() -> dict[str, list[str]]:
    return {
        "publication_ids": [],
        "project_ids": [],
        "location_ids": [],
    }


class TranscriptSource(str, enum.Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class TranscriptFormat(str, enum.Enum):
    VTT = "VTT"
    SRT = "SRT"
    PLAIN = "PLAIN"


class VideoStatus(str, enum.Enum):
    INGESTED = "INGESTED"
    TRANSCODING = "TRANSCODING"
    READY = "READY"
    FAILED = "FAILED"


class ClipStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class AnnotationReviewStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class TuningReportStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED_FOR_LIVE_COMPARISON = "APPROVED_FOR_LIVE_COMPARISON"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Platform admin: may grant/revoke authoring project_role rows. Bootstrapped
    # to the first (single-user MVP) account; orthogonal to project-scoped roles.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    website_slug: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Object(Base):
    __tablename__ = "objects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(String(2048))
    website_object_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_embed_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("objects.id", ondelete="SET NULL"))
    stable_video_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256_checksum: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # Identity of the normalized public playback representation.  The source
    # checksum above identifies the preservation/upload asset and therefore
    # cannot be used as a validator for the independently encoded MP4.  These
    # nullable, additive fields let legacy rows continue to load while a gated
    # backfill records the exact output identity once per artifact.
    playback_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    playback_file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    playback_recipe_version: Mapped[str | None] = mapped_column(String(64))
    playback_color_policy: Mapped[str | None] = mapped_column(String(64))
    playback_storage_key: Mapped[str | None] = mapped_column(String(255))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[VideoStatus] = mapped_column(Enum(VideoStatus, name="video_status"), default=VideoStatus.INGESTED)
    transcode_progress_pct: Mapped[int | None] = mapped_column(Integer)
    transcode_stage: Mapped[str | None] = mapped_column(String(64))
    transcode_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("video_id", "source", name="uq_transcript_video_source"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, server_default="Transcript")
    source: Mapped[TranscriptSource] = mapped_column(Enum(TranscriptSource, name="transcript_source"), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="en")
    format: Mapped[TranscriptFormat] = mapped_column(Enum(TranscriptFormat, name="transcript_format"), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ObjectModel(Base):
    __tablename__ = "object_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position_x: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    position_y: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    position_z: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    rotation_x: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    rotation_y: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    rotation_z: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    default_camera_json: Mapped[dict | None] = mapped_column(JSONB)
    public_poster_path: Mapped[str | None] = mapped_column(String(1024))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # when True, a successful publish of this package enqueues a
    # digest email to subscribers in the same customer_key partition.
    # Default False (opt-in) for explicit operator control.
    notify_followers_on_publish: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ObjectModelVariant(Base):
    """Approved, optimized derivative of a canonical ``ObjectModel`` (e.g. a
    mobile-texture build). DARK BY DEFAULT: a row only becomes a public-selectable
    model if it is approved (four-eyes), passes visual + device QA, is explicitly
    flagged selectable, and its ``source_canonical_sha256`` still matches the live
    canonical model. The public read path additionally requires the global
    ``public_model_variants_enabled`` kill switch to be on. The variant key is
    DB-constrained to an allowlist that can never contain ``ios-low-memory``.

    Portable column types (``Uuid``/``JSON``) so the constraint set is exercisable
    on SQLite in tests as well as Postgres in production.
    """

    __tablename__ = "object_model_variant"
    __table_args__ = (
        UniqueConstraint("object_model_id", "variant_key", name="uq_object_model_variant_model_key"),
        # DB-level guarantee: ``ios-low-memory`` (and any unknown key) can never be
        # stored. Mirrors the resolver allowlist.
        CheckConstraint(
            "variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192')",
            name="ck_object_model_variant_key_allowlist",
        ),
        CheckConstraint(
            "visual_qa_status IN ('pending', 'passed', 'failed')",
            name="ck_object_model_variant_visual_qa_status",
        ),
        CheckConstraint(
            "device_qa_status IN ('pending', 'passed', 'failed')",
            name="ck_object_model_variant_device_qa_status",
        ),
        CheckConstraint(
            "approval_status IN ('draft', 'submitted', 'approved', 'rejected', 'revoked')",
            name="ck_object_model_variant_approval_status",
        ),
        # Core invariant: a variant cannot be public-selectable unless it is fully
        # approved and both QA gates passed.
        CheckConstraint(
            "NOT is_public_selectable OR "
            "(approval_status = 'approved' AND visual_qa_status = 'passed' AND device_qa_status = 'passed')",
            name="ck_object_model_variant_selectable_requires_approval",
        ),
        # Four-eyes: the approver must differ from the author.
        CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by",
            name="ck_object_model_variant_four_eyes_distinct",
        ),
        # Four-eyes (presence): an approved variant must carry an approver
        # identity. With the selectable check (selectable => approved) and the
        # distinctness check above, a selectable row always has a non-null
        # approver distinct from the author.
        CheckConstraint(
            "approval_status <> 'approved' OR approved_by IS NOT NULL",
            name="ck_object_model_variant_approved_requires_approver",
        ),
        Index(
            "ix_object_model_variant_selectable",
            "object_model_id",
            "variant_key",
            postgresql_where=text("is_public_selectable"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("object_models.id", ondelete="CASCADE"), nullable=False
    )
    variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False, server_default=text("'model/gltf-binary'"))

    # Binding to the canonical model — the stale-invalidation key.
    source_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Asset metrics (map 1:1 to the optimization REPORT / QA report).
    variant_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_texture_dimension_px: Mapped[int] = mapped_column(Integer, nullable=False)
    decoded_texture_ram_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    triangle_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vertex_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    draw_call_count: Mapped[int | None] = mapped_column(Integer)
    geometry_extensions: Mapped[list | None] = mapped_column(JSON, nullable=False, server_default=text("'[]'"))

    # Generator recipe (reproducibility).
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_recipe: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Visual QA.
    visual_qa_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    visual_qa_changed_px_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    visual_qa_mean_delta: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    visual_qa_report_path: Mapped[str | None] = mapped_column(String(1024))
    visual_qa_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    visual_qa_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Device QA (physical device sign-off).
    device_qa_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    device_qa_notes: Mapped[str | None] = mapped_column(Text)
    device_qa_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    device_qa_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Approval (four-eyes).
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'draft'"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    # The only row-level flag the public read path consults (with the kill switch).
    is_public_selectable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ObjectModelDeliveryProfile(Base):
    """Per-object public delivery policy over fixed, server-allowlisted tiers.

    Profiles contain tokens and activation state only. Artifact identity, QA,
    approval, and immutable paths remain on ``ObjectModelVariant`` rows.
    """

    __tablename__ = "object_model_delivery_profiles"
    __table_args__ = (
        UniqueConstraint("object_model_id", name="uq_object_model_delivery_profile_model"),
        CheckConstraint(
            "standard_variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192')",
            name="ck_object_model_delivery_profile_standard_key",
        ),
        CheckConstraint(
            "constrained_variant_key IN ('mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192')",
            name="ck_object_model_delivery_profile_constrained_key",
        ),
        CheckConstraint(
            "standard_variant_key <> constrained_variant_key",
            name="ck_object_model_delivery_profile_distinct_keys",
        ),
        CheckConstraint(
            "NOT is_active OR (activated_by IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_object_model_delivery_profile_active_provenance",
        ),
        Index(
            "ix_object_model_delivery_profile_active",
            "object_model_id",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("object_models.id", ondelete="CASCADE"), nullable=False
    )
    standard_variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    constrained_variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("transcript_id", "position", name="uq_segment_transcript_position"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_vector: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_vector_dimensions))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TranscriptWindow(Base):
    __tablename__ = "transcript_windows"
    __table_args__ = (UniqueConstraint("transcript_id", "window_index", name="uq_transcript_window_index"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False
    )
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_position: Mapped[int] = mapped_column(Integer, nullable=False)
    end_position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_vector: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_vector_dimensions))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class VisualWindowDescription(Base):
    __tablename__ = "visual_window_descriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transcript_window_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transcript_windows.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    description_text: Mapped[str | None] = mapped_column(Text)
    embedding_vector: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_vector_dimensions))
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))
    frame_manifest_json: Mapped[dict | None] = mapped_column(JSONB)
    generator_provider: Mapped[str | None] = mapped_column(String(64))
    generator_model: Mapped[str | None] = mapped_column(String(255))
    embedding_provider: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TitleCardObservation(Base):
    __tablename__ = "title_card_observations"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "timestamp_ms",
            "source_kind",
            "source_version",
            "prompt_version",
            name="uq_title_card_observations_video_timestamp_version",
        ),
        Index("ix_title_card_observations_video_timestamp", "video_id", "timestamp_ms"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    title_card_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    object_name: Mapped[str | None] = mapped_column(Text)
    object_accession_number: Mapped[str | None] = mapped_column(Text)
    object_holding_institution: Mapped[str | None] = mapped_column(Text)
    object_city: Mapped[str | None] = mapped_column(Text)
    object_region: Mapped[str | None] = mapped_column(Text)
    object_country: Mapped[str | None] = mapped_column(Text)
    presenter_name: Mapped[str | None] = mapped_column(Text)
    presenter_age: Mapped[int | None] = mapped_column(Integer)
    presenter_city: Mapped[str | None] = mapped_column(Text)
    presenter_region: Mapped[str | None] = mapped_column(Text)
    presenter_country: Mapped[str | None] = mapped_column(Text)
    public_speaker_label: Mapped[str | None] = mapped_column(Text)
    session_date: Mapped[date | None] = mapped_column(Date)
    session_date_text: Mapped[str | None] = mapped_column(Text)
    additional_text: Mapped[str | None] = mapped_column(Text)
    raw_text_observed: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB)
    confidence_object: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    confidence_presenter: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    confidence_session_date: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(String(32))
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="title_card_ocr")
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ObjectModelAnnotation(Base):
    __tablename__ = "object_model_annotations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_models.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    video_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    clip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("clips.id", ondelete="SET NULL"))
    transcript_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    point_x: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    point_y: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    point_z: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    normal_x: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    normal_y: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    normal_z: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    camera_json: Mapped[dict | None] = mapped_column(JSONB)
    playlist_json: Mapped[list[dict] | None] = mapped_column(JSONB)
    website_annotation_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    website_relations_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=_default_annotation_relations)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[AnnotationReviewStatus] = mapped_column(
        Enum(AnnotationReviewStatus, name="annotation_review_status"),
        nullable=False,
        default=AnnotationReviewStatus.ACTIVE,
    )
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_revision_created_against: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelProvider(Base):
    __tablename__ = "model_providers"
    __table_args__ = (UniqueConstraint("user_id", "provider_name", name="uq_model_provider_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_providers.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    video_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="SET NULL"))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    output_mp4_path: Mapped[str | None] = mapped_column(String(1024))
    metadata_json_path: Mapped[str | None] = mapped_column(String(1024))
    website_clip_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    transcript_excerpt: Mapped[str | None] = mapped_column(Text)
    citation_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ClipStatus] = mapped_column(Enum(ClipStatus, name="clip_status"), default=ClipStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvidenceCollection(Base):
    __tablename__ = "evidence_collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    items_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PublicationManifest(Base):
    __tablename__ = "publication_manifests"
    # Manifest rows are durable package-history snapshots, so source UUID references stay
    # decoupled from live object/project rows instead of enforcing DB-level foreign keys.
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_publication_manifests_schema_version"),
        CheckConstraint("package_type = 'object_package'", name="ck_publication_manifests_package_type"),
        CheckConstraint("root_entity_type = 'object'", name="ck_publication_manifests_root_entity_type"),
        Index(
            "uq_publication_manifests_current_package",
            "package_type",
            "root_public_id",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
        Index("ix_publication_manifests_root_entity_id", "root_entity_id"),
        Index("ix_publication_manifests_projection_digest", "projection_digest"),
        Index("ix_publication_manifests_generated_at", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    package_type: Mapped[str] = mapped_column(String(64), nullable=False)
    root_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    root_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_public_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_public_slug: Mapped[str | None] = mapped_column(String(255))
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)
    public_title: Mapped[str | None] = mapped_column(String(255))
    public_summary: Mapped[str | None] = mapped_column(Text)
    public_payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    internal_linkage_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    media_manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    projection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_environment: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_environment: Mapped[str] = mapped_column(String(64), nullable=False)
    source_release_tag: Mapped[str | None] = mapped_column(String(255))
    actor_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    # Audit rows are append-only history records, so subject and manifest identifiers stay
    # decoupled from live rows instead of enforcing DB-level foreign keys.
    __table_args__ = (
        Index("ix_audit_events_created_at", "created_at"),
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
        Index("ix_audit_events_manifest_id", "publication_manifest_id"),
        Index("ix_audit_events_root_public_id", "root_public_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    publication_manifest_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    root_public_id: Mapped[str | None] = mapped_column(String(255))
    actor_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SearchQueryLog(Base):
    __tablename__ = "search_query_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    video_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("videos.id", ondelete="SET NULL"))
    query_text: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    query_source: Mapped[str] = mapped_column(String(32), nullable=False, default="analysis")
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lexical_result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic_result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_provider: Mapped[str | None] = mapped_column(String(64))
    semantic_model: Mapped[str | None] = mapped_column(String(255))
    retrieval_mode: Mapped[str | None] = mapped_column(String(32))
    top_semantic_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    top_window_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    top_surfaced_segment_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SearchResultFeedback(Base):
    __tablename__ = "search_result_feedback"
    __table_args__ = (UniqueConstraint("search_query_log_id", "segment_id", name="uq_search_result_feedback_query_segment"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    search_query_log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("search_query_logs.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("segments.id", ondelete="CASCADE"), nullable=False)
    is_relevant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lexical_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    rank_score: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    rank_position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SearchTuningReport(Base):
    __tablename__ = "search_tuning_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[TuningReportStatus] = mapped_column(
        Enum(TuningReportStatus, name="tuning_report_status"),
        nullable=False,
        default=TuningReportStatus.DRAFT,
    )
    feedback_window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    new_vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canonical_query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recurring_canonical_query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negative_vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CorpusPhrase(Base):
    __tablename__ = "corpus_phrases"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "normalized_phrase", "source", name="uq_corpus_phrases_project_phrase_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    phrase_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_phrase: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_vector: Mapped[list[float] | None] = mapped_column(Vector(768))
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    corpus_density: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# Relationships
User.projects = relationship("Project", backref="owner", cascade="all, delete-orphan")
User.model_providers = relationship("ModelProvider", backref="user", cascade="all, delete-orphan")
User.password_reset_tokens = relationship("PasswordResetToken", backref="user", cascade="all, delete-orphan")
User.object_models = relationship("ObjectModel", backref="uploaded_by_user", cascade="all, delete-orphan")
User.object_model_annotations = relationship("ObjectModelAnnotation", backref="created_by_user", cascade="all, delete-orphan")
User.search_query_logs = relationship("SearchQueryLog", backref="user", cascade="all, delete-orphan")
User.search_result_feedback = relationship("SearchResultFeedback", backref="user", cascade="all, delete-orphan")
User.search_tuning_reports = relationship("SearchTuningReport", backref="user", cascade="all, delete-orphan")

Project.objects = relationship("Object", backref="project", cascade="all, delete-orphan")
Project.videos = relationship("Video", backref="project", cascade="all, delete-orphan")
Project.clips = relationship("Clip", backref="project", cascade="all, delete-orphan")
Project.evidence_collections = relationship("EvidenceCollection", backref="project", cascade="all, delete-orphan")
Project.search_query_logs = relationship("SearchQueryLog", backref="project_ref")

Object.videos = relationship("Video", backref="object_ref")
Object.model = relationship("ObjectModel", backref="object", uselist=False, cascade="all, delete-orphan")
Object.model_annotations = relationship("ObjectModelAnnotation", backref="object", cascade="all, delete-orphan")

Video.transcripts = relationship("Transcript", backref="video", cascade="all, delete-orphan")
Video.clips = relationship("Clip", backref="video", cascade="all, delete-orphan")
Video.object_model_annotations = relationship("ObjectModelAnnotation", backref="video", cascade="all, delete-orphan")
Video.search_query_logs = relationship("SearchQueryLog", backref="video_ref")

Transcript.segments = relationship("Segment", backref="transcript", cascade="all, delete-orphan")
Transcript.windows = relationship("TranscriptWindow", backref="transcript", cascade="all, delete-orphan")
Segment.object_model_annotations = relationship("ObjectModelAnnotation", backref="transcript_segment")
Segment.search_result_feedback = relationship("SearchResultFeedback", backref="segment", cascade="all, delete-orphan")
SearchQueryLog.feedback_entries = relationship("SearchResultFeedback", backref="search_query_log", cascade="all, delete-orphan")

ObjectModel.annotations = relationship("ObjectModelAnnotation", backref="object_model", cascade="all, delete-orphan")

ModelProvider.model_runs = relationship("ModelRun", backref="provider")
Clip.object_model_annotations = relationship("ObjectModelAnnotation", backref="clip")


class Subscriber(Base):
    """Notify-me subscriber for the public landing page.

    Public subscriber registration. Partner-scoped via
    customer_key (always derived server-side from the request Host header
    via app.core.customer_keys.resolve_customer_key — never from a
    client-supplied request field).

    Soft-delete via unsubscribed_at. Hard-delete via the GDPR endpoint
    drops the row entirely. The unsubscribe_token is the only credential
    for both flows; treat it as bearer-secret.

    See apps/api/app/schemas/subscribers.py for request/response shape
    and the data-minimization posture.
    """

    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    customer_key: Mapped[str] = mapped_column(String(64), nullable=False)
    unsubscribe_token: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent_class: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unsubscribed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_digest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "email", "customer_key", name="uq_subscribers_email_customer"
        ),
        Index("ix_subscribers_customer_key", "customer_key"),
        Index("ix_subscribers_email", "email"),
    )
