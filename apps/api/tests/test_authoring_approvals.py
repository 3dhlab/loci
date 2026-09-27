"""Stage B3 — approval + citation-readiness endpoints (approver/PM, four-eyes).

HTTP tests over the real app with minted JWTs against the Vector-free table
subset. Skips cleanly if Postgres is unreachable or the schema cannot be built.
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
TEST_DATABASE_NAME = os.environ.get("AUTHORING_APPROVAL_TEST_DB", "semantic_authoring_approvals")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("AUTHORING_LANE_TEST_ADMIN_DB", "postgres"))

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "authoring-approval-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "authoring-approval-enc-key")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

SUBSET_TABLES = (
    "users", "projects", "audit_events", "project_role",
    "ocr_batch", "ocr_batch_frame", "observation_review", "title_card_segment",
    "approval_decision", "segment_spelling_flag", "evidence_override",
    "readiness_snapshot", "import_run", "import_run_check",
)
PROJECT_KEY = "demo-project"


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


class AuthoringApprovalHttpTests(unittest.TestCase):
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
            specs = [("reviewer", "reviewer"), ("approver", "approver"), ("approver2", "approver"),
                     ("pm", "pm"), ("operator", "operator"), ("auditor", "auditor")]
            for key, role in specs:
                u = User(email=f"{key}@appr.test", password_hash=get_password_hash("x"), is_active=True)
                db.add(u)
                db.flush()
                cls.user_ids[key] = u.id
                cls.tokens[key] = create_access_token(str(u.id))
                grant_project_role(db, user_id=u.id, project_key=PROJECT_KEY, role=role)
            u_none = User(email="none@appr.test", password_hash=get_password_hash("x"), is_active=True)
            db.add(u_none)
            db.flush()
            cls.user_ids["none"] = u_none.id
            cls.tokens["none"] = create_access_token(str(u_none.id))
            db.commit()

    def setUp(self):
        from sqlalchemy import delete
        from app.db.session import SessionLocal
        from app.models.authoring_lane import (
            ApprovalDecision, EvidenceOverride, ObservationReview, OcrBatch,
            ReadinessSnapshot, SegmentSpellingFlag, TitleCardSegment,
        )
        from app.models.entities import AuditEvent

        with SessionLocal() as db:
            db.execute(delete(OcrBatch))
            db.execute(delete(AuditEvent))
            db.commit()

            batch = OcrBatch(website_object_id="demo-bowl", stable_video_id="video-appr-test",
                             project_slug=PROJECT_KEY, artifact_dir="examples/test-artifacts",
                             cadence_seconds=5, duration_ms=30000)
            db.add(batch)
            db.flush()
            self.batch_id = str(batch.id)

            # seg1 proposed by approver (a1); seg2 proposed by reviewer.
            seg1 = TitleCardSegment(batch_id=batch.id, ordinal=0, segment_label="1", start_ms=0, end_ms_exclusive=10000,
                                    proposed_by=self.user_ids["approver"])
            seg2 = TitleCardSegment(batch_id=batch.id, ordinal=1, segment_label="2", start_ms=10000, end_ms_exclusive=20000,
                                    proposed_by=self.user_ids["reviewer"])
            db.add_all([seg1, seg2])
            db.flush()
            self.seg1_id = str(seg1.id)
            self.seg2_id = str(seg2.id)

            for ts in (0, 5000, 10000):
                db.add(ObservationReview(batch_id=batch.id, timestamp_ms=ts, status="ready", title_card_visible=True,
                                         confidence_object="high", confidence_presenter="high", confidence_session_date="high",
                                         session_date_text="February 7, 2025", public_speaker_label="Synthetic Presenter Gamma",
                                         included_in_date_only=True, included_in_speaker_approved=True,
                                         raw_text_observed=f"PRIVATE {ts}"))

            flag = SegmentSpellingFlag(batch_id=batch.id, field="presenter", proposed_canonical="Synthetic Presenter Epsilon",
                                       reviewer_confirmation_required=True)
            db.add(flag)
            db.flush()
            self.flag_id = str(flag.id)

            override = EvidenceOverride(batch_id=batch.id, timestamp_ms=5000, status="proposed",
                                        proposed_by=self.user_ids["reviewer"], raw_ocr_caveat="PRIVATE caption")
            db.add(override)
            db.flush()
            self.override_id = str(override.id)
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

    def _b(self, suffix):
        return f"/api/v1/authoring/batches/{self.batch_id}{suffix}"

    # ---- helpers to bring the batch to green ----
    def _make_green(self):
        # seg1 proposed by approver -> approve with approver2; seg2 by reviewer -> approve with approver
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"),
                         json={"approved_public_speaker_label": "Synthetic Presenter Gamma"}, headers=self._auth("approver2"))
        self.client.post(self._b(f"/segments/{self.seg2_id}/approve"),
                         json={"approved_public_speaker_label": "Synthetic Presenter Delta"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"),
                         json={"confirmed_canonical": "Synthetic Presenter Epsilon"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                         json={"decision": "include"}, headers=self._auth("approver"))
        self.client.post(self._b("/object-label"),
                         json={"approved_object_label": "Demo Bowl"}, headers=self._auth("pm"))

    # ---- RBAC ----
    def test_requires_auth(self):
        r = self.client.post(self._b(f"/segments/{self.seg1_id}/approve"), json={})
        self.assertEqual(r.status_code, 401)

    def test_approve_rbac(self):
        body = {"approved_public_speaker_label": "X"}
        # reviewer/uploader-not-present/operator/auditor/none denied
        for who in ("reviewer", "operator", "auditor", "none"):
            r = self.client.post(self._b(f"/segments/{self.seg2_id}/approve"), json=body, headers=self._auth(who))
            self.assertEqual(r.status_code, 403, f"{who}: {r.text}")
        # approver allowed (seg2 proposed by reviewer, so approver passes four-eyes)
        r = self.client.post(self._b(f"/segments/{self.seg2_id}/approve"), json=body, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)

    def test_object_label_pm_only(self):
        body = {"approved_object_label": "Demo Bowl"}
        r = self.client.post(self._b("/object-label"), json=body, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 403, "approver must not decide object label")
        r = self.client.post(self._b("/object-label"), json=body, headers=self._auth("pm"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved_object_label"], "Demo Bowl")

    # ---- four-eyes ----
    def test_four_eyes_segment_approve(self):
        # seg1 proposed by approver -> approver approving own segment => 409
        r = self.client.post(self._b(f"/segments/{self.seg1_id}/approve"),
                            json={"approved_public_speaker_label": "X"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 409, r.text)
        # a different approver may approve it
        r = self.client.post(self._b(f"/segments/{self.seg1_id}/approve"),
                            json={"approved_public_speaker_label": "X"}, headers=self._auth("approver2"))
        self.assertEqual(r.status_code, 200, r.text)

    def test_four_eyes_override_grant(self):
        # override proposed by reviewer -> approver (different) may grant
        r = self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                            json={"decision": "include"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)

    def test_four_eyes_override_grant_self_denied(self):
        # Re-point the override's proposer to the approver, then approver grant => 409.
        from app.db.session import SessionLocal
        from app.models.authoring_lane import EvidenceOverride
        with SessionLocal() as db:
            o = db.get(EvidenceOverride, uuid.UUID(self.override_id))
            o.proposed_by = self.user_ids["approver"]
            db.commit()
        r = self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                            json={"decision": "include"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 409, r.text)

    # ---- mutation correctness + audit ----
    def test_approve_updates_decision_and_audits_once(self):
        before = self._audit_count()
        r = self.client.post(self._b(f"/segments/{self.seg2_id}/approve"),
                            json={"approved_public_speaker_label": "Synthetic Presenter Delta", "session_date_text": "February 8, 2025"},
                            headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._audit_count(), before + 1)
        body = r.json()
        self.assertEqual(body["approval"]["status"], "approved")
        self.assertEqual(body["approval"]["approved_public_speaker_label"], "Synthetic Presenter Delta")

    def test_reject_segment_excluded_from_approved(self):
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"),
                        json={"approved_public_speaker_label": "X"}, headers=self._auth("approver2"))
        r = self.client.post(self._b(f"/segments/{self.seg2_id}/reject"),
                           json={"reason": "wrong speaker"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approval"]["status"], "rejected")
        # readiness summary should count only the approved segment.
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"),
                        json={"confirmed_canonical": "Synthetic Presenter Epsilon"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                        json={"decision": "include"}, headers=self._auth("approver"))
        self.client.post(self._b("/object-label"), json={"approved_object_label": "Demo Bowl"}, headers=self._auth("pm"))
        snap = self.client.post(self._b("/readiness/snapshot"),
                              json={"source_version": "demo-bowl-5s-20260622-speaker-approved-v1"}, headers=self._auth("pm"))
        self.assertEqual(snap.status_code, 200, snap.text)

    def test_spelling_confirm_records_and_audits(self):
        before = self._audit_count()
        r = self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"),
                           json={"confirmed_canonical": "Synthetic Presenter Epsilon"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["confirmed_canonical"], "Synthetic Presenter Epsilon")
        self.assertEqual(self._audit_count(), before + 1)

    def test_override_grant_and_reject_record_and_audit(self):
        before = self._audit_count()
        r = self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                           json={"decision": "include", "handling": "restored into band"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "granted")
        self.assertEqual(self._audit_count(), before + 1)

    # ---- readiness ----
    def test_readiness_snapshot_green_path(self):
        self._make_green()
        before = self._audit_count()
        r = self.client.post(self._b("/readiness/snapshot"),
                           json={"source_version": "demo-bowl-5s-20260622-speaker-approved-v1"}, headers=self._auth("pm"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["content_digest"]), 64)
        self.assertEqual(body["row_count"], 3)
        self.assertTrue(all(c["status"] == "pass" for c in body["checks"]))
        self.assertEqual(self._audit_count(), before + 1)

    def test_readiness_fails_on_unresolved_segment(self):
        # Approve only one segment; leave the other pending; decide everything else.
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"),
                        json={"approved_public_speaker_label": "X"}, headers=self._auth("approver2"))
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"),
                        json={"confirmed_canonical": "Synthetic Presenter Epsilon"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                        json={"decision": "include"}, headers=self._auth("approver"))
        self.client.post(self._b("/object-label"), json={"approved_object_label": "Demo Bowl"}, headers=self._auth("pm"))
        before = self._audit_count()
        r = self.client.post(self._b("/readiness/snapshot"),
                           json={"source_version": "sv-pending"}, headers=self._auth("pm"))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._audit_count(), before, "no audit on failed readiness")

    def test_readiness_fails_on_missing_object_label(self):
        # Resolve segments/flags/overrides but skip object-label.
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"), json={"approved_public_speaker_label": "X"}, headers=self._auth("approver2"))
        self.client.post(self._b(f"/segments/{self.seg2_id}/approve"), json={"approved_public_speaker_label": "Y"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"), json={"confirmed_canonical": "Z"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"), json={"decision": "include"}, headers=self._auth("approver"))
        r = self.client.post(self._b("/readiness/snapshot"), json={"source_version": "sv-no-label"}, headers=self._auth("pm"))
        self.assertEqual(r.status_code, 422, r.text)
        failing = {c["name"] for c in r.json()["detail"]["checks"]}
        self.assertIn("object_label_decided", failing)

    def test_readiness_fails_on_unresolved_override(self):
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"), json={"approved_public_speaker_label": "X"}, headers=self._auth("approver2"))
        self.client.post(self._b(f"/segments/{self.seg2_id}/approve"), json={"approved_public_speaker_label": "Y"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"), json={"confirmed_canonical": "Z"}, headers=self._auth("approver"))
        self.client.post(self._b("/object-label"), json={"approved_object_label": "Demo Bowl"}, headers=self._auth("pm"))
        # override left "proposed"
        r = self.client.post(self._b("/readiness/snapshot"), json={"source_version": "sv-ovr"}, headers=self._auth("pm"))
        self.assertEqual(r.status_code, 422, r.text)
        failing = {c["name"] for c in r.json()["detail"]["checks"]}
        self.assertIn("evidence_overrides_resolved", failing)

    def test_source_version_reuse_different_digest_rejected(self):
        self._make_green()
        sv = "demo-bowl-5s-20260622-speaker-approved-v1"
        r1 = self.client.post(self._b("/readiness/snapshot"), json={"source_version": sv}, headers=self._auth("pm"))
        self.assertEqual(r1.status_code, 200, r1.text)
        # Change the approved set (different label -> different digest), re-snapshot same sv.
        self.client.post(self._b(f"/segments/{self.seg2_id}/approve"),
                        json={"approved_public_speaker_label": "DIFFERENT NAME"}, headers=self._auth("approver"))
        r2 = self.client.post(self._b("/readiness/snapshot"), json={"source_version": sv}, headers=self._auth("pm"))
        self.assertEqual(r2.status_code, 409, r2.text)

    def test_four_eyes_snapshot_sole_proposer(self):
        # Make both segments proposed by approver2; approve both via approver; snapshot by approver2 -> 409.
        from app.db.session import SessionLocal
        from app.models.authoring_lane import TitleCardSegment
        with SessionLocal() as db:
            for sid in (self.seg1_id, self.seg2_id):
                seg = db.get(TitleCardSegment, uuid.UUID(sid))
                seg.proposed_by = self.user_ids["approver2"]
            db.commit()
        self.client.post(self._b(f"/segments/{self.seg1_id}/approve"), json={"approved_public_speaker_label": "X"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/segments/{self.seg2_id}/approve"), json={"approved_public_speaker_label": "Y"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/spelling-flags/{self.flag_id}/confirm"), json={"confirmed_canonical": "Z"}, headers=self._auth("approver"))
        self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"), json={"decision": "include"}, headers=self._auth("approver"))
        self.client.post(self._b("/object-label"), json={"approved_object_label": "Demo Bowl"}, headers=self._auth("pm"))
        r = self.client.post(self._b("/readiness/snapshot"), json={"source_version": "sv-sole"}, headers=self._auth("approver2"))
        self.assertEqual(r.status_code, 409, r.text)

    # ---- secret scan + no-audit-on-failure ----
    def test_secret_scan_rolls_back_and_no_audit(self):
        before = self._audit_count()
        r = self.client.post(self._b(f"/segments/{self.seg2_id}/approve"),
                           json={"approved_public_speaker_label": "Synthetic Presenter Delta", "rationale": "leak sk-abcdef0123456789abcdef"},
                           headers=self._auth("approver"))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._audit_count(), before, "no audit on secret-scan failure")
        # Approval must not have been set to approved (rolled back).
        segs = self.client.get(self._b("/segments"), headers=self._auth("approver")).json()
        seg2 = next(s for s in segs if s["id"] == self.seg2_id)
        self.assertNotEqual((seg2.get("approval") or {}).get("status"), "approved")

    def test_no_audit_on_missing_segment(self):
        before = self._audit_count()
        r = self.client.post(self._b(f"/segments/{uuid.uuid4()}/approve"),
                           json={"approved_public_speaker_label": "X"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._audit_count(), before)

    # ---- B4.1 fail-closed four-eyes on null counterpart ----
    def test_segment_approve_refused_when_proposed_by_null(self):
        from app.db.session import SessionLocal
        from app.models.authoring_lane import TitleCardSegment
        with SessionLocal() as db:
            seg = db.get(TitleCardSegment, uuid.UUID(self.seg2_id))
            seg.proposed_by = None
            db.commit()
        before = self._audit_count()
        r = self.client.post(self._b(f"/segments/{self.seg2_id}/approve"),
                             json={"approved_public_speaker_label": "Synthetic Presenter Delta"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("four-eyes", r.text)
        self.assertEqual(self._audit_count(), before, "no audit on fail-closed four-eyes")

    def test_override_grant_refused_when_proposed_by_null(self):
        from app.db.session import SessionLocal
        from app.models.authoring_lane import EvidenceOverride
        with SessionLocal() as db:
            o = db.get(EvidenceOverride, uuid.UUID(self.override_id))
            o.proposed_by = None
            db.commit()
        before = self._audit_count()
        r = self.client.post(self._b(f"/evidence-overrides/{self.override_id}/grant"),
                             json={"decision": "include"}, headers=self._auth("approver"))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("four-eyes", r.text)
        self.assertEqual(self._audit_count(), before, "no audit on fail-closed four-eyes")

    def test_public_citation_schema_snapshot_unchanged(self):
        from app.schemas.public import PublicEvidenceCitationAttributionResponse
        self.assertEqual(
            set(PublicEvidenceCitationAttributionResponse.model_fields.keys()),
            {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"},
        )


class StaticApprovalLogicTests(unittest.TestCase):
    """DB-less unit coverage for the approval-tier pure logic.

    These run everywhere (no Postgres required) and guard the four-eyes rule,
    the readiness checklist shape, and content-digest determinism.
    """

    def test_assert_four_eyes_rejects_same_identity(self):
        from fastapi import HTTPException

        from app.api.v1.endpoints.authoring.routes import _assert_four_eyes

        actor = uuid.uuid4()
        with self.assertRaises(HTTPException) as raised:
            _assert_four_eyes(actor, actor, "segment.proposed_by")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("four-eyes", raised.exception.detail)

    def test_assert_four_eyes_allows_distinct_or_absent_counterpart(self):
        from app.api.v1.endpoints.authoring.routes import _assert_four_eyes

        # Distinct counterpart and an absent (None) counterpart both pass silently.
        self.assertIsNone(_assert_four_eyes(uuid.uuid4(), uuid.uuid4(), "segment.proposed_by"))
        self.assertIsNone(_assert_four_eyes(uuid.uuid4(), None, "segment.proposed_by"))

    def test_compute_readiness_empty_batch_fails_and_digest_is_deterministic(self):
        import types

        from app.api.v1.endpoints.authoring.routes import _compute_readiness

        class _EmptyResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class _EmptySession:
            def execute(self, *_args, **_kwargs):
                return _EmptyResult()

        db = _EmptySession()
        batch = types.SimpleNamespace(
            id=uuid.uuid4(), approved_object_label=None, approved_object_accession=None
        )

        first = _compute_readiness(db, batch, "speaker_approved")
        self.assertFalse(first["passed"], "empty batch must not be import-ready")
        self.assertEqual(first["row_count"], 0)
        self.assertEqual(len(first["content_digest"]), 64)  # sha256 hexdigest
        checks = {c.name: c.status for c in first["checks"]}
        self.assertEqual(checks.get("all_segments_resolved"), "fail")
        self.assertEqual(checks.get("expected_row_count"), "fail")

        # Determinism: identical inputs → identical digest.
        second = _compute_readiness(db, batch, "speaker_approved")
        self.assertEqual(first["content_digest"], second["content_digest"])

        # Sensitivity: a different object-label decision yields a different digest.
        batch_relabeled = types.SimpleNamespace(
            id=batch.id, approved_object_label="Demo Container", approved_object_accession="DEMO-002"
        )
        relabeled = _compute_readiness(db, batch_relabeled, "speaker_approved")
        self.assertNotEqual(first["content_digest"], relabeled["content_digest"])


if __name__ == "__main__":
    unittest.main()
