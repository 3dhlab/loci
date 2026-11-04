"""Durable real-destination publication requests and fenced worker attempts."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PublicationJob(Base):
    __tablename__ = "publication_jobs"
    __table_args__ = (
        UniqueConstraint("actor_id", "destination", "request_key_hash", name="uq_publication_job_request"),
        CheckConstraint("destination = 'configured-public' AND lane = 'global'", name="ck_publication_job_destination"),
        CheckConstraint("status IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION','SUCCEEDED','FAILED','CANCELLED','SUPERSEDED')", name="ck_publication_job_status"),
        CheckConstraint("length(request_key_hash) = 64 AND length(body_fingerprint) = 64 AND length(selection_digest) = 64", name="ck_publication_job_digests"),
        CheckConstraint("attempt_number >= 0", name="ck_publication_job_attempt_number"),
        CheckConstraint("progress_percent >= 0 AND progress_percent <= 100", name="ck_publication_job_progress"),
        CheckConstraint("status NOT IN ('SUCCEEDED','FAILED','CANCELLED','SUPERSEDED') OR finished_at IS NOT NULL", name="ck_publication_job_terminal_time"),
        CheckConstraint("status NOT IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION') OR finished_at IS NULL", name="ck_publication_job_active_time"),
        CheckConstraint("selection_ref = CAST(id AS TEXT)", name="ck_publication_job_selection_ref"),
        CheckConstraint("status <> 'RUNNING' OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL)", name="ck_publication_job_running"),
        CheckConstraint("status <> 'SUCCEEDED' OR (receipt_json IS NOT NULL AND finished_at IS NOT NULL AND effect_started_at IS NOT NULL)", name="ck_publication_job_success"),
        CheckConstraint("status <> 'NEEDS_RECONCILIATION' OR effect_started_at IS NOT NULL", name="ck_publication_job_uncertain_effect"),
        CheckConstraint("status <> 'CANCELLED' OR ((effect_started_at IS NULL AND receipt_json IS NULL) OR COALESCE((effect_started_at IS NOT NULL AND receipt_json->>'version' = '1' AND receipt_json->>'outcome' = 'not_applied' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND receipt_json->>'reason' IN ('destination_changed','explicit_cancel')), false))", name="ck_publication_job_cancel_fence"),
        CheckConstraint("status <> 'SUPERSEDED' OR COALESCE((effect_started_at IS NOT NULL AND finished_at IS NOT NULL AND receipt_json->>'outcome' = 'superseded' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND jsonb_typeof(receipt_json->'current_head') = 'object' AND jsonb_typeof(receipt_json->'published_receipt') = 'object'), false)", name="ck_publication_job_superseded_resolution"),
        CheckConstraint("reconciliation_token IS NULL OR (status = 'NEEDS_RECONCILIATION' AND lease_expires_at IS NOT NULL)", name="ck_publication_job_reconciliation_claim"),
        CheckConstraint("status <> 'SUCCEEDED' OR COALESCE((jsonb_typeof(receipt_json) = 'object' AND receipt_json->>'version' = '1' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND receipt_json->>'records_sha256' ~ '^[0-9a-f]{64}$' AND receipt_json->>'media_sha256' ~ '^[0-9a-f]{64}$' AND receipt_json->>'selection_sha256' ~ '^[0-9a-f]{64}$' AND jsonb_typeof(receipt_json->'row_counts') = 'object' AND jsonb_typeof(receipt_json->'media_file_count') = 'number' AND receipt_json->'http_readback'->>'status' = 'verified'), false)", name="ck_publication_job_receipt_identity"),
        Index("uq_publication_job_active_lane", "lane", unique=True,
              postgresql_where=text("status IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION')")),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    request_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_metadata_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reconciliation_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effect_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage_key: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="Queued for publication.")
    error_code: Mapped[str | None] = mapped_column(String(64))
    receipt_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationGeneration(Base):
    """A destination commitment, written atomically with imported records."""
    __tablename__ = "publication_generations"
    __table_args__ = (
        CheckConstraint("operation_id = generation_id", name="ck_publication_generation_identity"),
        CheckConstraint("length(selection_digest) = 64", name="ck_publication_generation_digest"),
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    selection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PublicationDestinationHead(Base):
    __tablename__ = "publication_destination_head"
    __table_args__ = (
        CheckConstraint("lane = 'global'", name="ck_publication_destination_head_lane"),
        CheckConstraint("version >= 0", name="ck_publication_destination_head_version"),
    )
    lane: Mapped[str] = mapped_column(String(16), primary_key=True)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("publication_generations.generation_id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
