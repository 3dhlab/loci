"""Synthetic schema, public privacy boundary, and relationship tests for the authoring lane."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))


def _replace_database_name(database_url: str, database_name: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", parts.query, parts.fragment))


BASE_DATABASE_URL = os.environ.get(
    "AUTHORING_LANE_TEST_BASE_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://semantic:semantic@postgres:5432/semantic"),
)
TEST_DATABASE_NAME = os.environ.get("AUTHORING_LANE_TEST_DB", "semantic_authoring_lane")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("AUTHORING_LANE_TEST_ADMIN_DB", "postgres"))

# Configure the app to use the dedicated test DB before importing app modules.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "authoring-lane-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "authoring-lane-encryption-key")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")

# Private fields that must NEVER appear in any public citation payload field name.
PRIVATE_FIELD_NAMES = frozenset({
    "raw_text_observed",
    "additional_text",
    "presenter_name_raw",
    "raw_payload_json",
    "object_name_raw",
    "confidence_object",
    "confidence_presenter",
    "confidence_session_date",
    "rationale",
    "raw_ocr_caveat",
    "source_version",
    "source_kind",
    "model_run_id",
    "model_name",
    "model_provider",
})

MIGRATION_PATH = API_ROOT / "alembic" / "versions" / "20260619_0026_add_authoring_review_lane.py"
LANE_TABLES = (
    "ocr_batch",
    "ocr_batch_frame",
    "observation_review",
    "title_card_segment",
    "approval_decision",
    "segment_spelling_flag",
    "evidence_override",
    "import_run",
    "import_run_check",
)




def _load_migration_module():
    spec = importlib.util.spec_from_file_location("lane_migration_0026", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuthoringLaneStaticTests(unittest.TestCase):
    """No database required."""

    def test_migration_chains_from_title_card_observations(self):
        module = _load_migration_module()
        self.assertEqual(module.revision, "20260619_0026")
        self.assertEqual(module.down_revision, "20260528_0025")

    def test_migration_is_additive_creates_all_lane_tables(self):
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        # Every lane table is created in upgrade and dropped in downgrade.
        for table in LANE_TABLES:
            self.assertIn(f'create_table(\n        "{table}"', source, f"{table} not created")
            self.assertIn(f'drop_table("{table}")', source, f"{table} not dropped in downgrade")
        # Additive only: upgrade() must not alter/drop any pre-existing table.
        upgrade_src = source.split("def upgrade")[1].split("def downgrade")[0]
        for forbidden in ("op.drop_column", "op.alter_column", "op.drop_constraint", "op.drop_table"):
            self.assertNotIn(forbidden, upgrade_src, f"upgrade() must be additive; found {forbidden}")

    def test_public_citation_contract_excludes_private_fields(self):
        from app.schemas.public import (
            PublicEvidenceCitationAttributionResponse,
            PublicEvidenceCitationAttributionTimelineEntryResponse,
        )

        for model in (
            PublicEvidenceCitationAttributionResponse,
            PublicEvidenceCitationAttributionTimelineEntryResponse,
        ):
            field_names = set(model.model_fields.keys())
            leaked = field_names & PRIVATE_FIELD_NAMES
            self.assertEqual(leaked, set(), f"{model.__name__} leaks private fields: {leaked}")

    def test_public_citation_field_snapshot_unchanged(self):
        from app.schemas.public import PublicEvidenceCitationAttributionResponse

        self.assertEqual(
            set(PublicEvidenceCitationAttributionResponse.model_fields.keys()),
            {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"},
        )

    def test_public_evidence_service_has_no_lane_coupling(self):
        service_src = (API_ROOT / "app" / "services" / "public_evidence.py").read_text(encoding="utf-8")
        for name in ("authoring_lane", "OcrBatch", "ObservationReview", "ApprovalDecision", "EvidenceOverride", "ImportRun"):
            self.assertNotIn(name, service_src, f"public_evidence.py must not couple to lane symbol {name}")




def _can_connect() -> tuple[bool, str]:
    try:
        import psycopg
    except Exception as exc:  # noqa: BLE001
        return False, f"psycopg unavailable: {exc}"
    admin = ADMIN_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3):
            return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach Postgres at {urlsplit(admin).netloc}: {exc}"


class AuthoringLaneDbTests(unittest.TestCase):
    """Require a reachable Postgres; skipped cleanly otherwise."""

    @classmethod
    def setUpClass(cls):
        ok, reason = _can_connect()
        if not ok:
            raise unittest.SkipTest(reason)

        import psycopg
        from psycopg import sql

        admin = ADMIN_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (TEST_DATABASE_NAME,))
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE_NAME,))
            if cur.fetchone():
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

        # The lane tables are self-contained (no Vector columns, no FK to app
        # tables), so we create ONLY those nine tables. This keeps the lane test
        # independent of pgvector and the rest of the app schema.
        from app.db.session import Base, engine
        import app.models.authoring_lane  # noqa: F401  (registers lane metadata)

        cls.engine = engine
        lane_tables = [Base.metadata.tables[name] for name in LANE_TABLES]
        Base.metadata.create_all(bind=engine, tables=lane_tables)

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        import psycopg
        from psycopg import sql

        cls.engine.dispose()
        admin = ADMIN_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (TEST_DATABASE_NAME,))
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE_NAME,))
            if cur.fetchone():
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    def setUp(self):
        # Each test starts from an empty lane.
        from app.db.session import SessionLocal
        from app.models.authoring_lane import OcrBatch
        from sqlalchemy import delete

        with SessionLocal() as db:
            db.execute(delete(OcrBatch))
            db.commit()

    def test_models_and_relationships_with_cascade(self):
        from app.db.session import SessionLocal
        from app.models.authoring_lane import (
            ApprovalDecision,
            ImportRun,
            ImportRunCheck,
            ObservationReview,
            OcrBatch,
            OcrBatchFrame,
            TitleCardSegment,
        )
        from sqlalchemy import select

        with SessionLocal() as db:
            batch = OcrBatch(
                website_object_id="unit-obj",
                stable_video_id="video-unit-1",
                artifact_dir="examples/unit-artifacts",
                cadence_seconds=5,
            )
            db.add(batch)
            db.flush()
            db.add(OcrBatchFrame(batch_id=batch.id, frame_index=0, timestamp_ms=0))
            db.add(ObservationReview(batch_id=batch.id, timestamp_ms=0, raw_text_observed="PRIVATE",
                                     presenter_name_raw="Raw Name", included_in_speaker_approved=True,
                                     public_speaker_label="Approved Name"))
            segment = TitleCardSegment(batch_id=batch.id, ordinal=0, segment_label="1", start_ms=0, end_ms_exclusive=5000)
            db.add(segment)
            db.flush()
            db.add(ApprovalDecision(batch_id=batch.id, segment_id=segment.id,
                                    approved_public_speaker_label="Approved Name", approval_status="approved"))
            run = ImportRun(batch_id=batch.id, source_version="unit-obj-5s-speaker-approved-v1", inserted_count=1)
            db.add(run)
            db.flush()
            db.add(ImportRunCheck(import_run_id=run.id, check_name="unit", check_status="pass"))
            db.commit()
            batch_id = batch.id

        with SessionLocal() as db:
            loaded = db.get(OcrBatch, batch_id)
            self.assertEqual(len(loaded.frames), 1)
            self.assertEqual(len(loaded.observations), 1)
            self.assertEqual(loaded.segments[0].approval.approved_public_speaker_label, "Approved Name")
            self.assertEqual(loaded.import_runs[0].checks[0].check_status, "pass")

        # Cascade delete removes all children.
        with SessionLocal() as db:
            db.delete(db.get(OcrBatch, batch_id))
            db.commit()
        with SessionLocal() as db:
            self.assertEqual(db.execute(select(OcrBatchFrame)).scalars().all(), [])
            self.assertEqual(db.execute(select(ApprovalDecision)).scalars().all(), [])
            self.assertEqual(db.execute(select(ImportRunCheck)).scalars().all(), [])







if __name__ == "__main__":
    unittest.main()
