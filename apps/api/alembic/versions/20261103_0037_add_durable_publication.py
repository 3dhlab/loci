"""Durable real publication requests and destination generation commitments.

Revision ID: 20261103_0037
Revises: 20260813_0036
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261103_0037"
down_revision = "20260813_0036"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("publication_jobs",
        sa.Column("id",sa.Uuid(),primary_key=True),
        sa.Column("actor_id",sa.Uuid(),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("destination",sa.String(64),nullable=False),
        sa.Column("lane",sa.String(16),nullable=False),
        sa.Column("request_key_hash",sa.String(64),nullable=False),
        sa.Column("body_fingerprint",sa.String(64),nullable=False),
        sa.Column("selection_ref",sa.String(64),nullable=False),
        sa.Column("selection_digest",sa.String(64),nullable=False),
        sa.Column("selection_metadata_json",postgresql.JSONB(),nullable=False),
        sa.Column("status",sa.String(32),nullable=False),
        sa.Column("attempt_token",sa.Uuid()),
        sa.Column("reconciliation_token",sa.Uuid()),
        sa.Column("attempt_number",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("lease_expires_at",sa.DateTime(timezone=True)),
        sa.Column("effect_started_at",sa.DateTime(timezone=True)),
        sa.Column("progress_percent",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("stage_key",sa.String(64)),
        sa.Column("detail",sa.String(512),nullable=False),
        sa.Column("error_code",sa.String(64)),
        sa.Column("receipt_json",postgresql.JSONB()),
        sa.Column("last_dispatched_at",sa.DateTime(timezone=True)),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column("started_at",sa.DateTime(timezone=True)),
        sa.Column("finished_at",sa.DateTime(timezone=True)),
        sa.UniqueConstraint("actor_id","destination","request_key_hash",name="uq_publication_job_request"),
        sa.CheckConstraint("destination = 'configured-public' AND lane = 'global'",name="ck_publication_job_destination"),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION','SUCCEEDED','FAILED','CANCELLED','SUPERSEDED')",name="ck_publication_job_status"),
        sa.CheckConstraint("length(request_key_hash)=64 AND length(body_fingerprint)=64 AND length(selection_digest)=64",name="ck_publication_job_digests"),
        sa.CheckConstraint("attempt_number >= 0",name="ck_publication_job_attempt_number"),
        sa.CheckConstraint("progress_percent >= 0 AND progress_percent <= 100",name="ck_publication_job_progress"),
        sa.CheckConstraint("status NOT IN ('SUCCEEDED','FAILED','CANCELLED','SUPERSEDED') OR finished_at IS NOT NULL",name="ck_publication_job_terminal_time"),
        sa.CheckConstraint("status NOT IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION') OR finished_at IS NULL",name="ck_publication_job_active_time"),
        sa.CheckConstraint("selection_ref = CAST(id AS TEXT)",name="ck_publication_job_selection_ref"),
        sa.CheckConstraint("status <> 'RUNNING' OR (attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL)",name="ck_publication_job_running"),
        sa.CheckConstraint("status <> 'SUCCEEDED' OR (receipt_json IS NOT NULL AND finished_at IS NOT NULL AND effect_started_at IS NOT NULL)",name="ck_publication_job_success"),
        sa.CheckConstraint("status <> 'NEEDS_RECONCILIATION' OR effect_started_at IS NOT NULL",name="ck_publication_job_uncertain_effect"),
        sa.CheckConstraint("status <> 'CANCELLED' OR ((effect_started_at IS NULL AND receipt_json IS NULL) OR COALESCE((effect_started_at IS NOT NULL AND receipt_json->>'version' = '1' AND receipt_json->>'outcome' = 'not_applied' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND receipt_json->>'reason' IN ('destination_changed','explicit_cancel')), false))",name="ck_publication_job_cancel_fence"),
        sa.CheckConstraint("status <> 'SUPERSEDED' OR COALESCE((effect_started_at IS NOT NULL AND finished_at IS NOT NULL AND receipt_json->>'outcome' = 'superseded' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND jsonb_typeof(receipt_json->'current_head') = 'object' AND jsonb_typeof(receipt_json->'published_receipt') = 'object'), false)",name="ck_publication_job_superseded_resolution"),
        sa.CheckConstraint("reconciliation_token IS NULL OR (status = 'NEEDS_RECONCILIATION' AND lease_expires_at IS NOT NULL)",name="ck_publication_job_reconciliation_claim"),
        sa.CheckConstraint("status <> 'SUCCEEDED' OR COALESCE((jsonb_typeof(receipt_json) = 'object' AND receipt_json->>'version' = '1' AND receipt_json->>'operation_id' = CAST(id AS TEXT) AND receipt_json->>'generation_id' = CAST(id AS TEXT) AND receipt_json->>'selection_digest' = selection_digest AND receipt_json->>'records_sha256' ~ '^[0-9a-f]{64}$' AND receipt_json->>'media_sha256' ~ '^[0-9a-f]{64}$' AND receipt_json->>'selection_sha256' ~ '^[0-9a-f]{64}$' AND jsonb_typeof(receipt_json->'row_counts') = 'object' AND jsonb_typeof(receipt_json->'media_file_count') = 'number' AND receipt_json->'http_readback'->>'status' = 'verified'), false)",name="ck_publication_job_receipt_identity"),
        sa.CheckConstraint("jsonb_typeof(selection_metadata_json) = 'object'",name="ck_publication_job_selection_object"),
    )
    op.create_index("uq_publication_job_active_lane","publication_jobs",["lane"],unique=True,
                    postgresql_where=sa.text("status IN ('QUEUED','RUNNING','NEEDS_RECONCILIATION')"))
    op.create_table("publication_generations",
        sa.Column("operation_id",sa.Uuid(),primary_key=True),
        sa.Column("generation_id",sa.Uuid(),nullable=False,unique=True),
        sa.Column("selection_digest",sa.String(64),nullable=False),
        sa.Column("receipt_json",postgresql.JSONB(),nullable=False),
        sa.Column("committed_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("operation_id = generation_id",name="ck_publication_generation_identity"),
        sa.CheckConstraint("length(selection_digest)=64",name="ck_publication_generation_digest"),
        sa.CheckConstraint("jsonb_typeof(receipt_json)='object'",name="ck_publication_generation_receipt_object"),
    )
    op.create_table("publication_destination_head",
        sa.Column("lane",sa.String(16),primary_key=True),
        sa.Column("generation_id",sa.Uuid(),sa.ForeignKey("publication_generations.generation_id",ondelete="RESTRICT")),
        sa.Column("version",sa.BigInteger(),nullable=False,server_default="0"),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("lane = 'global'",name="ck_publication_destination_head_lane"),
        sa.CheckConstraint("version >= 0",name="ck_publication_destination_head_version"),
    )
    op.add_column("videos",sa.Column("publication_generation_id",sa.Uuid(),nullable=True))
    op.add_column("videos",sa.Column("publication_attribution_json",postgresql.JSONB(),nullable=True))
    op.create_foreign_key("fk_video_publication_generation","videos","publication_generations",
        ["publication_generation_id"],["generation_id"],ondelete="RESTRICT",deferrable=True,initially="DEFERRED")
    op.execute("""
    CREATE FUNCTION guard_publication_job() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'QUEUED' OR NEW.attempt_number <> 0 OR NEW.attempt_token IS NOT NULL
           OR NEW.reconciliation_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.effect_started_at IS NOT NULL OR NEW.receipt_json IS NOT NULL
           OR NEW.started_at IS NOT NULL OR NEW.finished_at IS NOT NULL OR NEW.progress_percent <> 0
           OR NEW.stage_key IS DISTINCT FROM 'queued' OR NEW.error_code IS NOT NULL OR NEW.last_dispatched_at IS NOT NULL
          THEN RAISE EXCEPTION 'publication requests must begin queued without an attempt or effect'; END IF;
        RETURN NEW;
      END IF;
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'publication history is retained'; END IF;
      IF (NEW.id,NEW.actor_id,NEW.destination,NEW.lane,NEW.request_key_hash,NEW.body_fingerprint,
          NEW.selection_ref,NEW.selection_digest,NEW.selection_metadata_json,NEW.created_at)
        IS DISTINCT FROM
         (OLD.id,OLD.actor_id,OLD.destination,OLD.lane,OLD.request_key_hash,OLD.body_fingerprint,
          OLD.selection_ref,OLD.selection_digest,OLD.selection_metadata_json,OLD.created_at)
        THEN RAISE EXCEPTION 'publication selection is immutable'; END IF;
      IF OLD.status IN ('SUCCEEDED','FAILED','CANCELLED','SUPERSEDED') AND NEW IS DISTINCT FROM OLD
        THEN RAISE EXCEPTION 'terminal publication history is immutable'; END IF;
      IF OLD.effect_started_at IS NOT NULL AND NEW.effect_started_at IS DISTINCT FROM OLD.effect_started_at
        THEN RAISE EXCEPTION 'publication effect admission is immutable'; END IF;
      IF OLD.effect_started_at IS NULL AND NEW.effect_started_at IS NOT NULL
         AND NOT (OLD.status='RUNNING' AND NEW.status='RUNNING')
        THEN RAISE EXCEPTION 'effect admission requires the current running phase'; END IF;
      IF OLD.status IN ('RUNNING','NEEDS_RECONCILIATION') AND NEW.status='CANCELLED'
         AND NOT COALESCE((NEW.effect_started_at IS NOT NULL AND NEW.receipt_json->>'outcome'='not_applied'),false)
        THEN RAISE EXCEPTION 'an admitted cancellation requires a destination fence'; END IF;
      IF OLD.status = NEW.status THEN
        IF NEW.attempt_token IS DISTINCT FROM OLD.attempt_token OR NEW.attempt_number <> OLD.attempt_number
          THEN RAISE EXCEPTION 'publication attempt identity is immutable'; END IF;
      ELSIF OLD.status='QUEUED' AND NEW.status='RUNNING' THEN
        IF NEW.attempt_token IS NULL OR NEW.attempt_number <> OLD.attempt_number + 1
          THEN RAISE EXCEPTION 'publication claim identity required'; END IF;
      ELSIF OLD.status='RUNNING' AND NEW.status='QUEUED' AND OLD.effect_started_at IS NULL THEN
        IF NEW.attempt_token IS NOT NULL OR NEW.attempt_number <> OLD.attempt_number OR NEW.lease_expires_at IS NOT NULL
          THEN RAISE EXCEPTION 'expired pre-effect recovery must release its old attempt'; END IF;
      ELSIF (OLD.status='QUEUED' AND NEW.status IN ('FAILED','CANCELLED')) OR
            (OLD.status='RUNNING' AND NEW.status IN ('SUCCEEDED','NEEDS_RECONCILIATION','CANCELLED','SUPERSEDED')) OR
            (OLD.status='RUNNING' AND NEW.status='FAILED' AND OLD.effect_started_at IS NULL) OR
            (OLD.status='NEEDS_RECONCILIATION' AND NEW.status IN ('SUCCEEDED','CANCELLED','SUPERSEDED')) THEN
        IF NEW.attempt_token IS DISTINCT FROM OLD.attempt_token OR NEW.attempt_number <> OLD.attempt_number
          THEN RAISE EXCEPTION 'publication attempt identity is immutable'; END IF;
      ELSE RAISE EXCEPTION 'invalid publication lifecycle transition'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER publication_job_guard BEFORE INSERT OR UPDATE OR DELETE ON publication_jobs
      FOR EACH ROW EXECUTE FUNCTION guard_publication_job();
    CREATE FUNCTION retain_publication_generation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'publication generation receipts are immutable'; END $$;
    CREATE TRIGGER publication_generation_guard BEFORE UPDATE OR DELETE ON publication_generations
      FOR EACH ROW EXECUTE FUNCTION retain_publication_generation();
    CREATE FUNCTION guard_publication_destination_head() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'publication destination head is retained'; END IF;
      IF NEW.lane IS DISTINCT FROM OLD.lane OR NEW.version <> OLD.version + 1
        THEN RAISE EXCEPTION 'publication head version must advance exactly once'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER publication_destination_head_guard BEFORE UPDATE OR DELETE ON publication_destination_head
      FOR EACH ROW EXECUTE FUNCTION guard_publication_destination_head();
    """)


def downgrade():
    bind=op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM publication_jobs) OR EXISTS(SELECT 1 FROM publication_generations) OR EXISTS(SELECT 1 FROM publication_destination_head)")):
        raise RuntimeError("Publication history exists. Retain this additive schema during code rollback; do not discard receipts.")
    op.execute("DROP TRIGGER publication_generation_guard ON publication_generations")
    op.execute("DROP FUNCTION retain_publication_generation()")
    op.execute("DROP TRIGGER publication_job_guard ON publication_jobs")
    op.execute("DROP FUNCTION guard_publication_job()")
    op.drop_constraint("fk_video_publication_generation","videos",type_="foreignkey")
    op.drop_column("videos","publication_attribution_json")
    op.drop_column("videos","publication_generation_id")
    op.execute("DROP TRIGGER publication_destination_head_guard ON publication_destination_head")
    op.execute("DROP FUNCTION guard_publication_destination_head()")
    op.drop_table("publication_destination_head")
    op.drop_table("publication_generations")
    op.drop_index("uq_publication_job_active_lane",table_name="publication_jobs")
    op.drop_table("publication_jobs")
