"""Stage B2 — reviewer/approver/PM proposal-write endpoints.

HTTP tests over the real FastAPI app with minted JWTs (no login endpoint, so no
Redis dependency) against the Vector-free table subset. Skips cleanly if
Postgres is unreachable or the schema cannot be built. Also includes static
secret-scan unit tests that always run.
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
TEST_DATABASE_NAME = os.environ.get("AUTHORING_WRITE_TEST_DB", "semantic_authoring_writes")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("AUTHORING_LANE_TEST_ADMIN_DB", "postgres"))

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "authoring-write-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "authoring-write-enc-key")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

SUBSET_TABLES = (
    "users", "projects", "audit_events", "project_role",
    "ocr_batch", "ocr_batch_frame", "observation_review", "title_card_segment",
    "approval_decision", "segment_spelling_flag", "evidence_override", "import_run", "import_run_check",
)
PROJECT_KEY = "demo-project"


class SecretScanStaticTests(unittest.TestCase):
    def test_secret_scan_flags_known_secrets(self):
        from app.api.v1.endpoints.authoring.audit import scan_for_secrets
        self.assertIn("openai_key", scan_for_secrets({"reason": "key sk-abcdef0123456789abcdef"}))
        self.assertIn("pg_url", scan_for_secrets("postgresql://u:p@host:5432/db"))
        self.assertIn("private_key_header", scan_for_secrets("-----BEGIN PRIVATE KEY-----"))
        self.assertIn("aws_access_key", scan_for_secrets("AKIAIOSFODNN7EXAMPLE"))
        self.assertIn("bearer_token", scan_for_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz"))
        # Clean text -> nothing.
        self.assertEqual(scan_for_secrets({"reason": "caption-only frame; no title card"}), [])

    def test_assert_no_secrets_raises(self):
        from app.api.v1.endpoints.authoring.audit import SecretLeakError, assert_no_secrets
        with self.assertRaises(SecretLeakError):
            assert_no_secrets({"note": "token sk-1234567890abcdef1234"})
        assert_no_secrets({"note": "perfectly innocent"})  # no raise


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


class AuthoringWriteHttpTests(unittest.TestCase):
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

        cls._seed_users_and_roles()

        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def _seed_users_and_roles(cls):
        from app.core.security import create_access_token, get_password_hash
        from app.db.session import SessionLocal
        from app.api.v1.endpoints.authoring.rbac import grant_project_role
        from app.models.entities import User

        cls.tokens = {}
        cls.user_ids = {}
        with SessionLocal() as db:
            # write-capable + write-denied roles, plus a no-role user
            for role in ("reviewer", "approver", "pm", "uploader", "operator", "auditor"):
                u = User(email=f"{role}@write.test", password_hash=get_password_hash("x"), is_active=True)
                db.add(u)
                db.flush()
                cls.user_ids[role] = u.id
                cls.tokens[role] = create_access_token(str(u.id))
                grant_project_role(db, user_id=u.id, project_key=PROJECT_KEY, role=role)
            u_none = User(email="none@write.test", password_hash=get_password_hash("x"), is_active=True)
            db.add(u_none)
            db.flush()
            cls.user_ids["none"] = u_none.id
            cls.tokens["none"] = create_access_token(str(u_none.id))
            db.commit()

    def setUp(self):
        # Fresh batch + lane rows + cleared audit log per test (users/roles persist).
        from sqlalchemy import delete
        from app.db.session import SessionLocal
        from app.models.authoring_lane import (
            ApprovalDecision, ObservationReview, OcrBatch, TitleCardSegment,
        )
        from app.models.entities import AuditEvent

        with SessionLocal() as db:
            db.execute(delete(OcrBatch))
            db.execute(delete(AuditEvent))
            db.commit()

            batch = OcrBatch(
                website_object_id="demo-bowl", stable_video_id="video-write-test",
                project_slug=PROJECT_KEY, artifact_dir="examples/test-artifacts",
                cadence_seconds=5, duration_ms=30000, source_version_speaker_approved="demo-bowl-5s-sa-v1",
            )
            db.add(batch)
            db.flush()
            self.batch_id = str(batch.id)
            for ts in (0, 5000, 10000):
                db.add(ObservationReview(
                    batch_id=batch.id, timestamp_ms=ts, status="ready", title_card_visible=True,
                    confidence_object="high", confidence_presenter="high", confidence_session_date="high",
                    session_date_text="February 7, 2025", public_speaker_label=None,
                    included_in_date_only=True, included_in_speaker_approved=False,
                    raw_text_observed=f"PRIVATE {ts}",
                ))
            seg = TitleCardSegment(batch_id=batch.id, ordinal=0, segment_label="1", start_ms=0, end_ms_exclusive=15000)
            db.add(seg)
            db.flush()
            self.proposed_segment_id = str(seg.id)
            approved_seg = TitleCardSegment(batch_id=batch.id, ordinal=1, segment_label="2", start_ms=15000, end_ms_exclusive=30000)
            db.add(approved_seg)
            db.flush()
            self.approved_segment_id = str(approved_seg.id)
            db.add(ApprovalDecision(batch_id=batch.id, segment_id=approved_seg.id, approval_status="approved",
                                    approved_public_speaker_label="Synthetic Presenter Gamma"))
            db.commit()

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

    def _audit_count(self):
        from sqlalchemy import func, select
        from app.db.session import SessionLocal
        from app.models.entities import AuditEvent
        with SessionLocal() as db:
            return db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

    # ---- auth + RBAC ----
    def test_writes_require_auth(self):
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0", json={"is_suppressed": True})
        self.assertEqual(r.status_code, 401, r.text)
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments", json={"segment_label": "x", "start_ms": 0, "end_ms_exclusive": 1000})
        self.assertEqual(r.status_code, 401, r.text)

    def test_write_rbac_allow_deny(self):
        body = {"is_suppressed": True, "suppression_reason": "reviewer note"}
        for who in ("reviewer", "approver", "pm"):
            r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0", json=body, headers=self._auth(who))
            self.assertEqual(r.status_code, 200, f"{who}: {r.text}")
        for who in ("uploader", "operator", "auditor", "none"):
            r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0", json=body, headers=self._auth(who))
            self.assertEqual(r.status_code, 403, f"{who}: {r.text}")

    def test_segment_create_rbac(self):
        body = {"segment_label": "seg-new", "start_ms": 0, "end_ms_exclusive": 5000}
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments", json=body, headers=self._auth("auditor"))
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments", json=body, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 201, r.text)

    # ---- audit semantics ----
    def test_audit_written_once_on_success(self):
        before = self._audit_count()
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0",
                              json={"is_suppressed": True}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._audit_count(), before + 1)
        from sqlalchemy import select
        from app.db.session import SessionLocal
        from app.models.entities import AuditEvent
        with SessionLocal() as db:
            ev = db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc())).scalars().first()
            self.assertEqual(ev.event_type, "authoring.observation.patch")
            self.assertEqual(ev.subject_type, "observation_review")
            self.assertIn("reviewer", ev.actor_json["roles"])
            self.assertEqual(ev.actor_json["project_key"], PROJECT_KEY)
            self.assertIn("before", ev.payload_json)
            self.assertIn("after", ev.payload_json)
            self.assertEqual(ev.payload_json["after"]["is_suppressed"], True)

    def test_no_audit_on_denied_rbac(self):
        before = self._audit_count()
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0",
                              json={"is_suppressed": True}, headers=self._auth("auditor"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._audit_count(), before)

    def test_no_audit_on_failed_validation(self):
        before = self._audit_count()
        # Unknown timestamp -> 404, no audit.
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/99999",
                              json={"is_suppressed": True}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 404)
        # Bad segment window -> 422, no audit.
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments",
                            json={"segment_label": "bad", "start_ms": 5000, "end_ms_exclusive": 5000}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 422)
        # Window beyond duration -> 422.
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments",
                            json={"segment_label": "toolong", "start_ms": 0, "end_ms_exclusive": 999999}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self._audit_count(), before)

    def test_secret_scan_rejects_and_rolls_back(self):
        before = self._audit_count()
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0",
                              json={"suppression_reason": "leak sk-abcdef0123456789abcdef"}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._audit_count(), before, "no audit on secret-scan rejection")
        # The field must NOT have been persisted (transaction rolled back).
        r2 = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth("reviewer"))
        row0 = next(o for o in r2.json() if o["timestamp_ms"] == 0)
        self.assertIsNone(row0["private"]["suppression_reason"])

    # ---- mutation correctness ----
    def test_observation_patch_updates_only_expected(self):
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/5000",
                              json={"is_suppressed": True, "suppression_reason": "caption-only frame"}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["is_suppressed"])
        self.assertEqual(body["review_state"], "caption_only")
        self.assertEqual(body["private"]["suppression_reason"], "caption-only frame")
        # public_speaker_label was not in the patch -> unchanged (None).
        self.assertIsNone(body["public_speaker_label"])

    def test_proposed_label_is_proposal_only_not_approval(self):
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0",
                              json={"proposed_public_speaker_label": "Proposed Name"}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["public_speaker_label"], "Proposed Name")
        # No approval was created/changed by an observation proposal.
        appr = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/approvals", headers=self._auth("reviewer")).json()
        # Only the pre-seeded approved segment approval exists; statuses unchanged.
        self.assertTrue(all(a["approval_status"] in ("approved",) for a in appr))

    def test_bulk_atomic_all_or_nothing(self):
        before = self._audit_count()
        # One bad timestamp -> whole request rejected, nothing changed.
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/observations/bulk",
                            json={"timestamps_ms": [0, 5000, 99999], "patch": {"is_suppressed": True}}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._audit_count(), before)
        rows = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth("reviewer")).json()
        self.assertFalse(any(o["is_suppressed"] for o in rows), "no row should be suppressed after failed bulk")

        # All valid -> applied, single audit event.
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/observations/bulk",
                            json={"timestamps_ms": [0, 5000, 10000], "patch": {"is_suppressed": True, "suppression_reason": "bulk"}}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["updated"], 3)
        self.assertEqual(self._audit_count(), before + 1, "one audit event for the bulk action")
        rows = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth("reviewer")).json()
        self.assertTrue(all(o["is_suppressed"] for o in rows))

    def test_bulk_payload_bounded(self):
        big = list(range(0, 5000 * 600, 5000))  # 600 > MAX_BULK_TIMESTAMPS
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/observations/bulk",
                            json={"timestamps_ms": big, "patch": {"is_suppressed": True}}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 422, "oversized bulk request must be rejected by validation")

    def test_segment_create_patch_delete_flow(self):
        # create
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments",
                            json={"segment_label": "seg-A", "start_ms": 0, "end_ms_exclusive": 6000, "raw_ocr_name": "Raw X"}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 201, r.text)
        seg_id = r.json()["id"]
        self.assertEqual(r.json()["private"]["raw_ocr_name"], "Raw X")
        # duplicate label -> 409
        r = self.client.post(f"/api/v1/authoring/batches/{self.batch_id}/segments",
                            json={"segment_label": "seg-A", "start_ms": 0, "end_ms_exclusive": 6000}, headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 409, r.text)
        # patch window
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/segments/{seg_id}",
                             json={"end_ms_exclusive": 7000, "session_date_text": "February 8, 2025"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["end_ms_exclusive"], 7000)
        # patch invalid window -> 422
        r = self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/segments/{seg_id}",
                             json={"start_ms": 8000, "end_ms_exclusive": 7000}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 422, r.text)
        # delete (pre-approval) -> 204
        r = self.client.delete(f"/api/v1/authoring/batches/{self.batch_id}/segments/{seg_id}", headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 204, r.text)

    def test_cannot_delete_approved_segment(self):
        r = self.client.delete(f"/api/v1/authoring/batches/{self.batch_id}/segments/{self.approved_segment_id}", headers=self._auth("pm"))
        self.assertEqual(r.status_code, 409, r.text)

    def test_source_version_immutable_after_writes(self):
        # No write endpoint exposes source_version; confirm it is unchanged after a mutation.
        self.client.patch(f"/api/v1/authoring/batches/{self.batch_id}/observations/0",
                          json={"is_suppressed": True}, headers=self._auth("reviewer"))
        from sqlalchemy import select
        from app.db.session import SessionLocal
        from app.models.authoring_lane import OcrBatch
        with SessionLocal() as db:
            batch = db.get(OcrBatch, uuid.UUID(self.batch_id))
            self.assertEqual(batch.source_version_speaker_approved, "demo-bowl-5s-sa-v1")

    def test_public_citation_schema_snapshot_unchanged(self):
        from app.schemas.public import PublicEvidenceCitationAttributionResponse
        self.assertEqual(
            set(PublicEvidenceCitationAttributionResponse.model_fields.keys()),
            {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"},
        )


if __name__ == "__main__":
    unittest.main()
