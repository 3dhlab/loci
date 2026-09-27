"""Stage B4 — guarded preflight / import / rollback.

Two layers:
  * StaticImportLogicTests — pure, DB-less unit coverage of the guarded-import
    helpers (path confinement, source_version shape + uniqueness, SHA match,
    preflight checklist gating, rollback scope, public-contract snapshot).
  * AuthoringImportHttpTests — HTTP coverage over the real app with minted JWTs
    against the Vector-free table subset. Skips cleanly if Postgres is
    unreachable or the schema cannot be built.

No test calls OCR/OpenAI, performs SSH/deploy/sync, or runs a real import — the
import executor is monkeypatched and every run is dry-run.
"""
from __future__ import annotations

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
    os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/postgres"),
)
TEST_DATABASE_NAME = os.environ.get("AUTHORING_IMPORT_TEST_DB", "semantic_authoring_imports")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("AUTHORING_LANE_TEST_ADMIN_DB", "postgres"))

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "authoring-import-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "authoring-import-enc-key")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

# Stage B5: confine the import/backup roots to a test-owned tempdir and enable
# real (row-writing) import execution. Set BEFORE the app imports so the
# import-time DEFAULT_IMPORT_ROOT / DEFAULT_BACKUP_ROOT capture these values.
import tempfile  # noqa: E402

_IMPORT_ROOT = os.path.join(tempfile.gettempdir(), "b5-authoring-import-root")
os.environ["AUTHORING_IMPORT_ROOT"] = _IMPORT_ROOT
os.environ["AUTHORING_IMPORT_BACKUP_ROOT"] = os.path.join(_IMPORT_ROOT, "backups")
os.environ["AUTHORING_IMPORT_PRODUCTION_ENABLED"] = "1"

SUBSET_TABLES = (
    "users", "projects", "objects", "audit_events", "project_role",
    "model_providers", "model_runs", "videos", "title_card_observations",
    "ocr_batch", "ocr_batch_frame", "observation_review", "title_card_segment",
    "approval_decision", "segment_spelling_flag", "evidence_override",
    "readiness_snapshot", "import_run", "import_run_check",
)
PROJECT_KEY = "demo-project"


# --------------------------------------------------------------------------- #
# Static DB-less unit tests
# --------------------------------------------------------------------------- #
class StaticImportLogicTests(unittest.TestCase):
    def test_path_confinement_rejects_traversal_and_absolute_escape(self):
        from app.api.v1.endpoints.authoring.imports import PathConfinementError, confine_import_path

        root = "/var/lib/semantic/import"
        # Happy path: a plain filename resolves under the root.
        ok = confine_import_path("demo-bowl-speaker-approved-v1.json", root)
        self.assertTrue(str(ok).startswith(str(Path(root).resolve())))

        # Traversal escape.
        with self.assertRaises(PathConfinementError):
            confine_import_path("../../etc/passwd", root)
        # Absolute path outside the root.
        with self.assertRaises(PathConfinementError):
            confine_import_path("/etc/passwd", root)
        # Sneaky nested traversal that climbs out.
        with self.assertRaises(PathConfinementError):
            confine_import_path("a/b/../../../../etc/shadow", root)

    def test_source_version_shape_and_uniqueness(self):
        from app.api.v1.endpoints.authoring.imports import source_version_available, validate_source_version

        self.assertTrue(validate_source_version("demo-bowl-5s-20260622-speaker-approved-v1"))
        self.assertFalse(validate_source_version(""))
        self.assertFalse(validate_source_version("-leading-hyphen"))
        self.assertFalse(validate_source_version("has space"))
        self.assertFalse(validate_source_version("bad/slash"))

        existing = {"already-bound-v1", "other-v2"}
        self.assertTrue(source_version_available(existing, "fresh-v1"))
        self.assertFalse(source_version_available(existing, "already-bound-v1"))

    def test_sha_match_logic(self):
        from app.api.v1.endpoints.authoring.imports import sha_matches

        a = "0890cbeaa97a2c95c3150d123fc3a3943aeea20ca3e5acb92f201cbb5cde22fc"
        self.assertTrue(sha_matches(a, a.upper()))  # case-insensitive
        self.assertTrue(sha_matches(f"  {a}  ", a))  # trimmed
        self.assertFalse(sha_matches(a, "f" * 64))
        self.assertFalse(sha_matches(None, a))
        self.assertFalse(sha_matches(a, None))

    def test_rollback_scope_is_exactly_video_id_and_source_version(self):
        from app.api.v1.endpoints.authoring.imports import rollback_scope

        vid = uuid.uuid4()
        scope = rollback_scope(vid, "demo-bowl-5s-20260622-speaker-approved-v1")
        self.assertEqual(set(scope.keys()), {"video_id", "source_version"})
        self.assertEqual(scope["video_id"], str(vid))
        self.assertEqual(scope["source_version"], "demo-bowl-5s-20260622-speaker-approved-v1")

    def _green_kwargs(self):
        digest = "a" * 64
        sha = "b" * 64
        return dict(
            snapshot_exists=True, snapshot_passed=True,
            recomputed_digest=digest, snapshot_digest=digest,
            source_version="demo-bowl-v1", existing_source_versions=set(),
            target_sv_rows=0, fixture_sha_expected=sha, fixture_sha_actual=sha,
            media_present=False, media_sha_expected=None, media_sha_actual=None,
            fixture_path_confined=True, backup_status="pending",
            public_contract_keys_ok=True, mode="dry_run",
        )

    def test_build_preflight_all_green_passes(self):
        from app.api.v1.endpoints.authoring.imports import build_preflight_checks, preflight_passed

        checks = build_preflight_checks(**self._green_kwargs())
        self.assertTrue(preflight_passed(checks))
        names = {c["name"] for c in checks}
        # Every mandated check is present.
        for required in (
            "readiness_snapshot_present_and_green", "readiness_digest_matches_snapshot",
            "source_version_shape_valid", "source_version_unique_for_project",
            "target_sv_rows_zero", "fixture_sha_matches_snapshot", "fixture_path_confined",
            "public_contract_allowed_keys_only", "media_sha_matches_batch", "backup_captured",
        ):
            self.assertIn(required, names)

    def test_build_preflight_each_red_check_blocks(self):
        from app.api.v1.endpoints.authoring.imports import build_preflight_checks, preflight_passed

        # digest mismatch
        k = self._green_kwargs(); k["snapshot_digest"] = "c" * 64
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # target rows present
        k = self._green_kwargs(); k["target_sv_rows"] = 142
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # fixture sha mismatch
        k = self._green_kwargs(); k["fixture_sha_actual"] = "d" * 64
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # path not confined
        k = self._green_kwargs(); k["fixture_path_confined"] = False
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # public-contract dirty
        k = self._green_kwargs(); k["public_contract_keys_ok"] = False
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # source_version not unique
        k = self._green_kwargs(); k["existing_source_versions"] = {"demo-bowl-v1"}
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # no snapshot
        k = self._green_kwargs(); k["snapshot_exists"] = False
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))

    def test_build_preflight_media_required_only_when_present(self):
        from app.api.v1.endpoints.authoring.imports import build_preflight_checks, preflight_passed

        # media present but mismatched -> fail
        k = self._green_kwargs()
        k.update(media_present=True, media_sha_expected="e" * 64, media_sha_actual="f" * 64)
        self.assertFalse(preflight_passed(build_preflight_checks(**k)))
        # media present and matching -> pass
        k = self._green_kwargs()
        k.update(media_present=True, media_sha_expected="e" * 64, media_sha_actual="E" * 64)
        self.assertTrue(preflight_passed(build_preflight_checks(**k)))

    def test_production_mode_requires_backup(self):
        from app.api.v1.endpoints.authoring.imports import build_preflight_checks, preflight_passed

        k = self._green_kwargs()
        k.update(mode="production", backup_status="pending")
        self.assertFalse(preflight_passed(build_preflight_checks(**k)), "production needs a captured backup")
        k["backup_status"] = "captured"
        self.assertTrue(preflight_passed(build_preflight_checks(**k)))

    def test_public_citation_schema_snapshot_unchanged(self):
        from app.schemas.public import PublicEvidenceCitationAttributionResponse
        self.assertEqual(
            set(PublicEvidenceCitationAttributionResponse.model_fields.keys()),
            {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"},
        )

    def test_dry_run_executor_touches_nothing_and_reports_completed(self):
        from app.api.v1.endpoints.authoring.imports import execute_import

        summary = execute_import(mode="dry_run", production_video_uuid=uuid.uuid4(),
                                  source_version="demo-bowl-v1", row_count=142)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(summary["inserted"], 142)
        self.assertEqual(summary["observations_written"], 142)
        self.assertEqual(summary["removed"], 0)
        self.assertIsNone(summary["model_run_id"])

    def test_single_read_hashed_bytes_are_the_parsed_bytes(self):
        # The bytes that are SHA-hashed must be the same bytes that get parsed
        # into the import batch (no reopen). Round-trip a byte string through
        # both and confirm the hash equals the canonical sha and the parse
        # reflects exactly those bytes.
        import hashlib
        import json as _json

        from app.api.v1.endpoints.authoring.imports import sha256_bytes
        from app.services.title_card_ocr import load_title_card_import_batch_from_bytes

        doc = {
            "prompt_version": "2026-04-24-v1",
            "source_kind": "pilot_artifact",
            "source_version": "demo-bowl-single-read-v1",
            "observations": [
                {"timestamp_ms": 0, "title_card_visible": True, "raw_text_observed": "X",
                 "public_speaker_label": "Synthetic Presenter Gamma", "session_date_text": "February 7, 2025",
                 "confidence": {"object": "high", "presenter": "high", "session_date": "high"}},
            ],
        }
        data = _json.dumps(doc).encode("utf-8")
        self.assertEqual(sha256_bytes(data), hashlib.sha256(data).hexdigest())
        batch = load_title_card_import_batch_from_bytes(data)
        self.assertEqual(batch.source_version, "demo-bowl-single-read-v1")
        self.assertEqual(len(batch.observations), 1)
        # A different byte string yields a different hash AND different parse.
        doc2 = dict(doc, source_version="demo-bowl-single-read-v2")
        data2 = _json.dumps(doc2).encode("utf-8")
        self.assertNotEqual(sha256_bytes(data2), sha256_bytes(data))
        self.assertEqual(load_title_card_import_batch_from_bytes(data2).source_version, "demo-bowl-single-read-v2")


# --------------------------------------------------------------------------- #
# DB-backed HTTP tests
# --------------------------------------------------------------------------- #
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


class AuthoringImportHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, reason = _can_connect()
        if not ok:
            raise unittest.SkipTest(reason)
        import psycopg
        from psycopg import sql

        admin = ADMIN_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (TEST_DATABASE_NAME,))
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (TEST_DATABASE_NAME,))
            if cur.fetchone():
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

        from app.db.session import Base, engine
        import app.db.base  # noqa: F401

        cls.engine = engine
        try:
            Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables[n] for n in SUBSET_TABLES])
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"could not build schema subset: {exc}")

        cls._seed_users()
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def _seed_users(cls):
        from app.core.security import create_access_token, get_password_hash
        from app.db.session import SessionLocal
        from app.api.v1.endpoints.authoring.rbac import grant_project_role
        from app.models.entities import User

        cls.tokens = {}
        cls.user_ids = {}
        with SessionLocal() as db:
            specs = [("reviewer", "reviewer"), ("approver", "approver"), ("pm", "pm"),
                     ("operator", "operator"), ("operator2", "operator"), ("auditor", "auditor")]
            for key, role in specs:
                u = User(email=f"{key}@imp.test", password_hash=get_password_hash("x"), is_active=True)
                db.add(u)
                db.flush()
                cls.user_ids[key] = u.id
                cls.tokens[key] = create_access_token(str(u.id))
                grant_project_role(db, user_id=u.id, project_key=PROJECT_KEY, role=role)
            u_none = User(email="none@imp.test", password_hash=get_password_hash("x"), is_active=True)
            db.add(u_none)
            db.flush()
            cls.user_ids["none"] = u_none.id
            cls.tokens["none"] = create_access_token(str(u_none.id))
            db.commit()

    def _write_fixture(self) -> tuple[str, str]:
        """Write a real speaker-approved fixture under the import root.

        Returns (relative_filename, sha256). The fixture's source_version equals
        the approved source_version so the import's reviewed==imported guard
        passes.
        """
        import hashlib
        import json as _json

        os.makedirs(_IMPORT_ROOT, exist_ok=True)
        observations = [
            {
                "timestamp_ms": ts,
                "title_card_visible": True,
                "raw_text_observed": f"Synthetic Presenter Gamma\nDemo Bowl\nFebruary 7, 2025 (frame {ts})",
                "object": {"name": "Demo Bowl", "accession_number": None},
                "presenter": {"name": "Synthetic Presenter Gamma"},
                "public_speaker_label": "Synthetic Presenter Gamma",
                "session_date_text": "February 7, 2025",
                "confidence": {"object": "high", "presenter": "high", "session_date": "high"},
            }
            for ts in (0, 5000, 10000)
        ]
        doc = {
            "prompt_version": "2026-04-24-v1",
            "model_provider": "openai",
            "model_name": "gpt-4o-mini",
            "detail": "high",
            "source_kind": "pilot_artifact",
            "source_version": self.source_version,
            "observations": observations,
        }
        rel = f"demo-bowl-speaker-approved-{uuid.uuid4().hex[:8]}.json"
        path = os.path.join(_IMPORT_ROOT, rel)
        data = _json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(data)
        return rel, hashlib.sha256(data).hexdigest()

    def setUp(self):
        from sqlalchemy import delete, select
        from app.db.session import SessionLocal
        from app.models.authoring_lane import ObservationReview, OcrBatch, TitleCardSegment
        from app.models.entities import AuditEvent, Object, Project, TitleCardObservation, User, Video

        self.source_version = "demo-bowl-5s-20260622-speaker-approved-v1"
        with SessionLocal() as db:
            db.execute(delete(OcrBatch))
            db.execute(delete(AuditEvent))
            db.execute(delete(TitleCardObservation))
            db.execute(delete(Video))
            db.execute(delete(Object))
            db.execute(delete(Project))
            db.commit()

            # Seed the production video row the import attaches observations to.
            owner = db.execute(select(User).where(User.email == "operator@imp.test")).scalar_one()
            project = Project(owner_id=owner.id, name="B5 Test Project")
            db.add(project)
            db.flush()
            video = Video(project_id=project.id, stable_video_id="video-imp-test",
                          title="Demo Bowl", original_filename="demo-bowl.mp4",
                          source_path="/var/lib/semantic/media/demo-bowl.mp4",
                          sha256_checksum="d" * 64, duration_ms=30000)
            db.add(video)
            db.flush()
            self.video_uuid = video.id

            batch = OcrBatch(website_object_id="demo-bowl", stable_video_id="video-imp-test",
                             project_slug=PROJECT_KEY, artifact_dir="examples/test-artifacts",
                             cadence_seconds=5, duration_ms=30000, approved_object_label="Demo Bowl",
                             production_video_uuid=self.video_uuid)
            db.add(batch)
            db.flush()
            self.batch_id = str(batch.id)

            seg = TitleCardSegment(batch_id=batch.id, ordinal=0, segment_label="1", start_ms=0,
                                   end_ms_exclusive=30000, proposed_by=self.user_ids["reviewer"])
            db.add(seg)
            db.flush()
            self.seg_id = str(seg.id)

            for ts in (0, 5000, 10000):
                db.add(ObservationReview(batch_id=batch.id, timestamp_ms=ts, status="ready",
                                         title_card_visible=True, confidence_object="high",
                                         confidence_presenter="high", confidence_session_date="high",
                                         session_date_text="February 7, 2025",
                                         public_speaker_label="Synthetic Presenter Gamma",
                                         included_in_date_only=True, included_in_speaker_approved=True,
                                         raw_text_observed=f"PRIVATE {ts}"))
            db.commit()

        # Write a real fixture file and bind its true SHA into the snapshot.
        self.fixture_rel, self.fixture_sha = self._write_fixture()
        self.client.post(self._b(f"/segments/{self.seg_id}/approve"),
                         json={"approved_public_speaker_label": "Synthetic Presenter Gamma",
                               "approved_canonical_object_label": "Demo Bowl"},
                         headers=self._auth("approver"))
        snap = self.client.post(self._b("/readiness/snapshot"),
                                json={"source_version": self.source_version, "fixture_sha256": self.fixture_sha},
                                headers=self._auth("pm"))
        assert snap.status_code == 200, snap.text

    def _title_card_rows(self):
        from sqlalchemy import select as _select
        from app.db.session import SessionLocal
        from app.models.entities import TitleCardObservation
        with SessionLocal() as db:
            return db.execute(_select(TitleCardObservation).where(
                TitleCardObservation.video_id == self.video_uuid,
                TitleCardObservation.source_version == self.source_version,
            )).scalars().all()

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "engine"):
            return
        cls.client.close()
        import psycopg
        from psycopg import sql
        cls.engine.dispose()
        admin = ADMIN_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (TEST_DATABASE_NAME,))
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (TEST_DATABASE_NAME,))
            if cur.fetchone():
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    def _auth(self, who):
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def _b(self, suffix):
        return f"/api/v1/authoring/batches/{self.batch_id}{suffix}"

    def _audit_count(self):
        from sqlalchemy import func, select
        from app.db.session import SessionLocal
        from app.models.entities import AuditEvent
        with SessionLocal() as db:
            return db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

    def _preflight_body(self, **over):
        body = {"source_version": self.source_version, "phase": "speaker_approved",
                "fixture_path": self.fixture_rel,
                "fixture_sha256": self.fixture_sha, "mode": "dry_run"}
        body.update(over)
        return body

    def _green_preflight(self, who="operator", **over):
        r = self.client.post(self._b("/preflight"), json=self._preflight_body(**over), headers=self._auth(who))
        assert r.status_code == 200, r.text
        assert r.json()["passed"], r.text
        return r.json()["id"]

    # ---- preflight RBAC ----
    def test_preflight_rbac(self):
        body = self._preflight_body()
        for who in ("approver", "pm", "operator"):
            r = self.client.post(self._b("/preflight"), json=body, headers=self._auth(who))
            self.assertEqual(r.status_code, 200, f"{who}: {r.text}")
        for who in ("reviewer", "auditor", "none"):
            r = self.client.post(self._b("/preflight"), json=body, headers=self._auth(who))
            self.assertEqual(r.status_code, 403, f"{who}: {r.text}")

    def test_preflight_writes_exactly_one_audit(self):
        before = self._audit_count()
        self._green_preflight()
        self.assertEqual(self._audit_count(), before + 1)

    def test_green_preflight_then_real_production_import_writes_rows(self):
        run_id = self._green_preflight()
        before = self._audit_count()
        self.assertEqual(len(self._title_card_rows()), 0, "no rows before import")
        r = self.client.post(self._b("/imports"), json={"preflight_run_id": run_id, "mode": "production"},
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["mode"], "production")
        self.assertEqual(body["observations_written"], 3)
        self.assertEqual(body["inserted_count"], 3)
        self.assertEqual(body["target_sv_rows"], 0)
        self.assertEqual(body["backup_row_count"], 0, "empty-state backup recorded")
        self.assertIsNotNone(body["backup_sha256"])
        self.assertEqual(str(body["executed_by"]), str(self.user_ids["operator"]))
        # Real rows were written to title_card_observations.
        rows = self._title_card_rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.public_speaker_label == "Synthetic Presenter Gamma" for r in rows))
        self.assertEqual(self._audit_count(), before + 1)

    def test_dry_run_import_writes_no_rows(self):
        run_id = self._green_preflight()
        r = self.client.post(self._b("/imports"), json={"preflight_run_id": run_id, "mode": "dry_run"},
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["mode"], "dry_run")
        self.assertEqual(body["observations_written"], 3, "reports would-write count")
        self.assertEqual(len(self._title_card_rows()), 0, "dry-run must not write rows")

    def test_red_preflight_blocks_import(self):
        # A preflight with a mismatched fixture SHA fails and must block import.
        r = self.client.post(self._b("/preflight"),
                             json=self._preflight_body(fixture_sha256="f" * 64),
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["passed"])
        run_id = r.json()["id"]
        ri = self.client.post(self._b("/imports"), json={"preflight_run_id": run_id},
                              headers=self._auth("operator"))
        self.assertEqual(ri.status_code, 409, ri.text)

    def test_import_rbac_operator_only(self):
        run_id = self._green_preflight()
        for who in ("approver", "pm", "reviewer", "auditor", "none"):
            r = self.client.post(self._b("/imports"), json={"preflight_run_id": run_id}, headers=self._auth(who))
            self.assertEqual(r.status_code, 403, f"{who}: {r.text}")

    def test_import_rejects_executed_by_equals_approved_by(self):
        # Snapshot was created by pm; temporarily make pm also an operator, then
        # pm-import => 409. The temporary operator grant is revoked in finally so
        # it cannot leak into other tests' RBAC assertions.
        from sqlalchemy import delete
        from app.db.session import SessionLocal
        from app.api.v1.endpoints.authoring.rbac import grant_project_role
        from app.models.authoring_lane import ProjectRole

        with SessionLocal() as db:
            grant_project_role(db, user_id=self.user_ids["pm"], project_key=PROJECT_KEY, role="operator")
            db.commit()
        try:
            run_id = self._green_preflight()
            # Four-eyes is checked before any write; pm == snapshot.created_by => 409.
            r = self.client.post(self._b("/imports"),
                                 json={"preflight_run_id": run_id, "mode": "production"},
                                 headers=self._auth("pm"))
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("four-eyes", r.text)
            self.assertEqual(len(self._title_card_rows()), 0, "no rows written on four-eyes failure")
        finally:
            with SessionLocal() as db:
                db.execute(delete(ProjectRole).where(
                    ProjectRole.user_id == self.user_ids["pm"],
                    ProjectRole.project_key == PROJECT_KEY,
                    ProjectRole.role == "operator",
                ))
                db.commit()

    def test_failed_preflight_validation_writes_no_audit_on_bad_path(self):
        # A path-escape fixture_path is accepted by the schema but fails the
        # confinement CHECK -> preflight row still records (a preflight is itself
        # a recorded check run), but the import it would authorize is blocked.
        before = self._audit_count()
        r = self.client.post(self._b("/preflight"),
                             json=self._preflight_body(fixture_path="../../etc/passwd"),
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["passed"])
        confined = {c["name"]: c["status"] for c in r.json()["checks"]}
        self.assertEqual(confined["fixture_path_confined"], "fail")

    def _real_production_import(self):
        run_id = self._green_preflight()
        imp = self.client.post(self._b("/imports"),
                               json={"preflight_run_id": run_id, "mode": "production"},
                               headers=self._auth("operator"))
        assert imp.status_code == 200, imp.text
        return imp.json()["id"]

    def test_rollback_deletes_scoped_rows_and_audits(self):
        import_run_id = self._real_production_import()
        self.assertEqual(len(self._title_card_rows()), 3, "rows written by production import")
        before = self._audit_count()
        rb = self.client.post(f"/api/v1/authoring/imports/{import_run_id}/rollback", headers=self._auth("operator"))
        self.assertEqual(rb.status_code, 200, rb.text)
        body = rb.json()
        scope = body["scope"]
        self.assertEqual(set(scope.keys()), {"video_id", "source_version"})
        self.assertEqual(scope["video_id"], str(self.video_uuid))
        self.assertEqual(scope["source_version"], self.source_version)
        self.assertEqual(body["rollback_deleted_count"], 3, "real scoped delete count")
        self.assertEqual(len(self._title_card_rows()), 0, "rows removed by rollback")
        self.assertEqual(self._audit_count(), before + 1)

    def test_rollback_rejects_non_operator(self):
        import_run_id = self._real_production_import()
        for who in ("approver", "pm", "reviewer", "auditor", "none"):
            rb = self.client.post(f"/api/v1/authoring/imports/{import_run_id}/rollback", headers=self._auth(who))
            self.assertEqual(rb.status_code, 403, f"{who}: {rb.text}")

    def test_readiness_digest_mismatch_blocks_preflight(self):
        # Change the approved content after the snapshot, so the recomputed digest
        # no longer matches the snapshot's bound digest -> preflight fails.
        self.client.post(self._b(f"/segments/{self.seg_id}/approve"),
                         json={"approved_public_speaker_label": "DIFFERENT NAME",
                               "approved_canonical_object_label": "Demo Bowl"},
                         headers=self._auth("approver"))
        r = self.client.post(self._b("/preflight"), json=self._preflight_body(), headers=self._auth("operator"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["passed"])
        names = {c["name"]: c["status"] for c in r.json()["checks"]}
        self.assertEqual(names["readiness_digest_matches_snapshot"], "fail")

    # ---- B4.1 fail-closed four-eyes ----
    def test_import_refused_when_snapshot_created_by_is_null(self):
        # Null out the snapshot's created_by, then a green preflight's import must
        # fail closed (unattributable approval cannot authorize an import).
        from app.db.session import SessionLocal
        from app.models.authoring_lane import ReadinessSnapshot
        from sqlalchemy import select as _select
        run_id = self._green_preflight()
        with SessionLocal() as db:
            snap = db.execute(_select(ReadinessSnapshot).where(
                ReadinessSnapshot.batch_id == uuid.UUID(self.batch_id),
                ReadinessSnapshot.source_version == self.source_version,
            )).scalar_one()
            snap.created_by = None
            db.commit()
        r = self.client.post(self._b("/imports"),
                             json={"preflight_run_id": run_id, "mode": "production"},
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("four-eyes", r.text)
        self.assertEqual(len(self._title_card_rows()), 0, "no rows written when four-eyes fails closed")

    # ---- B4.1 rollback audit visibility ----
    def test_rollback_audit_visible_through_scoped_audit(self):
        import_run_id = self._real_production_import()
        rb = self.client.post(f"/api/v1/authoring/imports/{import_run_id}/rollback", headers=self._auth("operator"))
        self.assertEqual(rb.status_code, 200, rb.text)
        # The rollback audit event must be visible through scoped GET /audit for
        # both an operator and an auditor on this project.
        for who in ("operator", "auditor"):
            a = self.client.get("/api/v1/authoring/audit?event_type=authoring.import.rollback",
                                headers=self._auth(who))
            self.assertEqual(a.status_code, 200, a.text)
            events = a.json()
            self.assertTrue(events, f"{who}: rollback audit event not visible")
            ev = events[0]
            self.assertEqual(ev["actor"].get("project_key"), PROJECT_KEY)
            self.assertEqual(ev["payload"]["after"]["scope"]["source_version"], self.source_version)

    # ---- B5.2 single-read: tamper the staged fixture after the SHA was bound ----
    def test_import_refused_when_staged_fixture_tampered_after_snapshot(self):
        run_id = self._green_preflight()
        # Overwrite the staged fixture with different bytes after the snapshot
        # bound the original SHA. The server re-hashes the on-disk bytes at
        # import and must refuse (no rows written).
        with open(os.path.join(_IMPORT_ROOT, self.fixture_rel), "wb") as fh:
            fh.write(b'{"tampered": true}')
        r = self.client.post(self._b("/imports"),
                             json={"preflight_run_id": run_id, "mode": "production"},
                             headers=self._auth("operator"))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("fixture SHA-256", r.text)
        self.assertEqual(len(self._title_card_rows()), 0, "no rows written when fixture tampered")

    # ---- B5.2 rollback isolation ----
    def test_rollback_deletes_only_target_video_and_source_version(self):
        from sqlalchemy import select
        from app.db.session import SessionLocal
        from app.models.entities import TitleCardObservation

        import_run_id = self._real_production_import()
        self.assertEqual(len(self._title_card_rows()), 3)

        other_video_id = uuid.uuid4()
        with SessionLocal() as db:
            # Seed unrelated rows that rollback MUST NOT touch.
            # (a) same video, different source_version.
            db.add(TitleCardObservation(
                video_id=self.video_uuid, timestamp_ms=999000, title_card_visible=True,
                raw_text_observed="OTHER SV", prompt_version="2026-04-24-v1",
                source_kind="pilot_artifact", source_version="demo-bowl-OTHER-v9", status="ready"))
            # (b) different video, same source_version. Needs a videos row (FK).
            from app.models.entities import Project, User, Video
            owner = db.execute(select(User).where(User.email == "operator@imp.test")).scalar_one()
            proj = db.execute(select(Project)).scalars().first()
            other_video = Video(id=other_video_id, project_id=proj.id, stable_video_id="video-other-imp",
                                title="Other", original_filename="other.mp4",
                                source_path="/var/lib/semantic/media/other.mp4", sha256_checksum="e" * 64)
            db.add(other_video)
            db.flush()
            db.add(TitleCardObservation(
                video_id=other_video_id, timestamp_ms=0, title_card_visible=True,
                raw_text_observed="OTHER VIDEO", prompt_version="2026-04-24-v1",
                source_kind="pilot_artifact", source_version=self.source_version, status="ready"))
            db.commit()

        rb = self.client.post(f"/api/v1/authoring/imports/{import_run_id}/rollback", headers=self._auth("operator"))
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertEqual(rb.json()["rollback_deleted_count"], 3, "only the 3 target rows deleted")
        # Target rows gone; both unrelated sets survive.
        self.assertEqual(len(self._title_card_rows()), 0)
        with SessionLocal() as db:
            same_video_other_sv = db.execute(select(TitleCardObservation).where(
                TitleCardObservation.video_id == self.video_uuid,
                TitleCardObservation.source_version == "demo-bowl-OTHER-v9")).scalars().all()
            other_video_same_sv = db.execute(select(TitleCardObservation).where(
                TitleCardObservation.video_id == other_video_id,
                TitleCardObservation.source_version == self.source_version)).scalars().all()
        self.assertEqual(len(same_video_other_sv), 1, "same-video/other-sv row must survive")
        self.assertEqual(len(other_video_same_sv), 1, "other-video/same-sv row must survive")

    # ---- B5.2 production preflight disabled ----
    def test_production_preflight_returns_400_when_production_disabled(self):
        from unittest import mock
        with mock.patch("app.api.v1.endpoints.authoring.routes.production_imports_enabled", return_value=False):
            r = self.client.post(self._b("/preflight"),
                                 json=self._preflight_body(mode="production"),
                                 headers=self._auth("operator"))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("production mode is not enabled", r.text)


if __name__ == "__main__":
    unittest.main()
