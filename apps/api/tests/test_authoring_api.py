"""Tests for the Stage B1 read-only authoring API + RBAC skeleton.

- ``AuthoringApiStaticTests`` need no DB/app: RBAC matrix logic, auditor
  exclusivity, will_publish_as parity vs the live public projection, the
  private/public field boundary, review-state classification, and the
  public-citation schema snapshot.
- ``AuthoringApiHttpTests`` build the (Vector-free) table subset the authoring
  routes need and drive the real FastAPI app with minted JWTs (no login
  endpoint, so no Redis dependency). They skip cleanly if Postgres is
  unreachable or the schema cannot be built.
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
TEST_DATABASE_NAME = os.environ.get("AUTHORING_API_TEST_DB", "semantic_authoring_api")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("AUTHORING_LANE_TEST_ADMIN_DB", "postgres"))

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "authoring-api-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "authoring-api-encryption-key")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

# Tables the authoring read routes (+ token auth) touch — none have Vector columns,
# so the suite runs against a plain Postgres without pgvector.
SUBSET_TABLES = (
    "users",
    "projects",
    "audit_events",
    "project_role",
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

PRIVATE_FIELD_NAMES = frozenset({
    "raw_text_observed", "additional_text", "presenter_name_raw", "object_name_raw",
    "raw_payload_json", "confidence_object", "confidence_presenter", "confidence_session_date",
    "suppression_reason", "rationale", "reviewer_source",
})


class AuthoringApiStaticTests(unittest.TestCase):
    def test_rbac_matrix_logic(self):
        from app.api.v1.endpoints.authoring.rbac import (
            AUDIT_READ_ROLES, MEMBER_ROLES, ROLES, is_authorized,
        )
        self.assertEqual(ROLES, {"uploader", "reviewer", "approver", "pm", "operator", "auditor"})
        # Every role is a member (can read batch-scoped data).
        for role in ROLES:
            self.assertTrue(is_authorized({role}, MEMBER_ROLES), f"{role} should be a member")
        # Audit read is restricted to oversight roles.
        for role in ("approver", "pm", "operator", "auditor"):
            self.assertTrue(is_authorized({role}, AUDIT_READ_ROLES), f"{role} should read audit")
        for role in ("uploader", "reviewer"):
            self.assertFalse(is_authorized({role}, AUDIT_READ_ROLES), f"{role} must NOT read audit")
        # No roles => denied everywhere.
        self.assertFalse(is_authorized(set(), MEMBER_ROLES))
        self.assertFalse(is_authorized(set(), AUDIT_READ_ROLES))

    def test_auditor_exclusivity(self):
        from app.api.v1.endpoints.authoring.rbac import auditor_exclusivity_violation
        # Granting auditor to someone holding a mutating role -> violation.
        self.assertTrue(auditor_exclusivity_violation({"reviewer"}, "auditor"))
        self.assertTrue(auditor_exclusivity_violation({"operator"}, "auditor"))
        # Granting a mutating role to an auditor -> violation.
        self.assertTrue(auditor_exclusivity_violation({"auditor"}, "reviewer"))
        # Auditor alongside auditor (idempotent-ish) or non-mutating combos -> ok.
        self.assertFalse(auditor_exclusivity_violation({"auditor"}, "auditor"))
        self.assertFalse(auditor_exclusivity_violation(set(), "auditor"))
        self.assertFalse(auditor_exclusivity_violation({"reviewer"}, "approver"))

    def _obs(self, **kw):
        from app.models.authoring_lane import ObservationReview
        defaults = dict(
            timestamp_ms=0, status="ready", title_card_visible=True,
            confidence_object="high", confidence_presenter="high", confidence_session_date="high",
            session_date_text="February 7, 2025", public_speaker_label=None,
            included_in_date_only=True, included_in_speaker_approved=False, is_suppressed=False,
            raw_text_observed="PRIVATE OCR TEXT", presenter_name_raw="Raw Presenter",
        )
        defaults.update(kw)
        return ObservationReview(**defaults)

    def test_will_publish_as_parity_named(self):
        from app.api.v1.endpoints.authoring.gating import compute_gates
        from app.services.public_evidence import _citation_attribution_payload
        from app.api.v1.endpoints.authoring.gating import observation_to_public_shape

        obs = self._obs(public_speaker_label="Synthetic Presenter Gamma", confidence_presenter="high", included_in_speaker_approved=True)
        gates = compute_gates(obs)
        self.assertTrue(gates["date_gate"])
        self.assertTrue(gates["speaker_gate"])
        self.assertEqual(gates["will_publish_as"], "named")
        # Parity: the preview equals what the live projection emits for the same shape.
        live = _citation_attribution_payload(observation_to_public_shape(obs))
        self.assertEqual(live["speaker_label"], "Synthetic Presenter Gamma")
        self.assertEqual(live["attribution_mode"], "named")
        self.assertEqual(gates["public_preview"], {
            "speaker_label": "Synthetic Presenter Gamma",
            "session_date_text": "February 7, 2025",
            "session_date_precision": "text",
            "attribution_mode": "named",
        })

    def test_will_publish_as_parity_dated_speakerless(self):
        # The Demo Bowl 130000 shape: reliable date, presenter confidence none.
        from app.api.v1.endpoints.authoring.gating import compute_gates, observation_to_public_shape
        from app.services.public_evidence import _citation_attribution_payload

        obs = self._obs(public_speaker_label="Synthetic Presenter Zeta", confidence_presenter="none", included_in_speaker_approved=True)
        gates = compute_gates(obs)
        self.assertTrue(gates["date_gate"], "date gate should pass")
        self.assertFalse(gates["speaker_gate"], "speaker gate must fail when presenter confidence is none")
        self.assertEqual(gates["will_publish_as"], "dated_speakerless")
        live = _citation_attribution_payload(observation_to_public_shape(obs))
        self.assertIsNone(live["speaker_label"], "live projection must withhold the speaker")
        self.assertEqual(live["attribution_mode"], "unavailable")

    def test_will_publish_as_unavailable_without_date(self):
        from app.api.v1.endpoints.authoring.gating import compute_gates
        obs = self._obs(title_card_visible=False, session_date_text=None, public_speaker_label=None)
        gates = compute_gates(obs)
        self.assertFalse(gates["date_gate"])
        self.assertFalse(gates["speaker_gate"])
        self.assertEqual(gates["will_publish_as"], "unavailable")

    def test_review_state_classification(self):
        from app.api.v1.endpoints.authoring.gating import compute_review_state
        self.assertEqual(compute_review_state(self._obs(status="failed"), set()), "failed")
        self.assertEqual(compute_review_state(self._obs(timestamp_ms=185000), {185000}), "restored")
        self.assertEqual(
            compute_review_state(self._obs(is_suppressed=True, suppression_reason="caption-only frame"), set()),
            "caption_only",
        )
        self.assertEqual(
            compute_review_state(self._obs(is_suppressed=True, suppression_reason="date_not_in_raw_text"), set()),
            "no_date",
        )
        self.assertEqual(
            compute_review_state(self._obs(is_suppressed=True, suppression_reason="excluded_from_date_only"), set()),
            "suppressed",
        )
        self.assertEqual(
            compute_review_state(self._obs(public_speaker_label="X", confidence_presenter="high", included_in_speaker_approved=True), set()),
            "approved",
        )
        self.assertEqual(compute_review_state(self._obs(), set()), "ready")

    def test_observation_schema_private_boundary(self):
        from app.api.v1.endpoints.authoring.schemas import ObservationResponse, ObservationPrivateBlock
        top_level = set(ObservationResponse.model_fields.keys())
        # Private OCR fields must NOT be top-level on the observation response.
        self.assertEqual(top_level & PRIVATE_FIELD_NAMES, set(), f"private fields leaked to top level: {top_level & PRIVATE_FIELD_NAMES}")
        self.assertIn("private", top_level)
        # And they must live inside the private block.
        private_fields = set(ObservationPrivateBlock.model_fields.keys())
        for name in ("raw_text_observed", "presenter_name_raw", "object_name_raw", "raw_payload_json",
                     "confidence_object", "confidence_presenter", "confidence_session_date", "suppression_reason"):
            self.assertIn(name, private_fields, f"{name} should be inside private block")

    def test_public_citation_schema_snapshot_unchanged(self):
        from app.schemas.public import PublicEvidenceCitationAttributionResponse
        self.assertEqual(
            set(PublicEvidenceCitationAttributionResponse.model_fields.keys()),
            {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"},
        )

    def test_authoring_router_not_under_public_prefix(self):
        from app.main import app
        # FastAPI 0.141 keeps included routers as nested route objects.  The
        # generated application contract is the stable compatibility surface.
        authoring_paths = [path for path in app.openapi()["paths"] if path.startswith("/api/v1/authoring")]
        self.assertTrue(authoring_paths, "authoring routes should be mounted under /api/v1/authoring")
        for path in authoring_paths:
            self.assertFalse(path.startswith("/api/v1/public"), f"authoring route {path} must not be under /public")


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


class AuthoringApiHttpTests(unittest.TestCase):
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
        import app.db.base  # noqa: F401  (registers all metadata)

        cls.engine = engine
        try:
            tables = [Base.metadata.tables[name] for name in SUBSET_TABLES]
            Base.metadata.create_all(bind=engine, tables=tables)
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"could not build schema subset: {exc}")

        cls._seed()

        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def _seed(cls):
        from app.core.security import create_access_token, get_password_hash
        from app.db.session import SessionLocal
        from app.api.v1.endpoints.authoring.rbac import grant_project_role
        from app.models.authoring_lane import (
            ApprovalDecision, EvidenceOverride, ObservationReview, OcrBatch, OcrBatchFrame, TitleCardSegment,
        )
        from app.models.entities import AuditEvent, User

        cls.tokens = {}
        cls.user_ids = {}
        with SessionLocal() as db:
            users = {}
            for key in ("none", "reviewer", "auditor", "operator"):
                u = User(email=f"{key}@authoring.test", password_hash=get_password_hash("x"), is_active=True)
                db.add(u)
                db.flush()
                users[key] = u.id
                cls.tokens[key] = create_access_token(str(u.id))
            # A platform admin (role-grant administrator) and a plain target user.
            admin = User(email="admin@authoring.test", password_hash=get_password_hash("x"),
                         is_active=True, is_platform_admin=True)
            target = User(email="target@authoring.test", password_hash=get_password_hash("x"), is_active=True)
            db.add_all([admin, target])
            db.flush()
            users["admin"] = admin.id
            users["target"] = target.id
            cls.tokens["admin"] = create_access_token(str(admin.id))
            cls.tokens["target"] = create_access_token(str(target.id))
            cls.user_ids = dict(users)

            # Batch with NO project_slug -> project_key falls back to stable_video_id.
            batch = OcrBatch(
                website_object_id="demo-container",
                stable_video_id="video-test-sample",
                project_slug=None,
                artifact_dir="examples/test-artifacts",
                cadence_seconds=5,
                expected_frame_count=3,
                duration_ms=15000,
            )
            db.add(batch)
            db.flush()
            cls.batch_id = str(batch.id)
            project_key = batch.project_slug or batch.stable_video_id

            db.add(OcrBatchFrame(batch_id=batch.id, frame_index=0, timestamp_ms=0))
            # named row
            db.add(ObservationReview(batch_id=batch.id, timestamp_ms=0, status="ready", title_card_visible=True,
                                     confidence_object="high", confidence_presenter="high", confidence_session_date="high",
                                     session_date_text="February 7, 2025", public_speaker_label="Synthetic Presenter Gamma",
                                     included_in_date_only=True, included_in_speaker_approved=True,
                                     raw_text_observed="PRIVATE A", presenter_name_raw="Raw Gamma"))
            # dated-speakerless row (presenter confidence none)
            db.add(ObservationReview(batch_id=batch.id, timestamp_ms=5000, status="ready", title_card_visible=True,
                                     confidence_object="high", confidence_presenter="none", confidence_session_date="high",
                                     session_date_text="February 7, 2025", public_speaker_label="Synthetic Presenter Zeta",
                                     included_in_date_only=True, included_in_speaker_approved=True,
                                     raw_text_observed="PRIVATE B", presenter_name_raw="Raw Zeta"))
            # suppressed caption-only row
            db.add(ObservationReview(batch_id=batch.id, timestamp_ms=10000, status="ready", title_card_visible=False,
                                     confidence_object="none", confidence_presenter="none", confidence_session_date="none",
                                     session_date_text=None, public_speaker_label=None,
                                     included_in_date_only=False, included_in_speaker_approved=False,
                                     is_suppressed=True, suppression_reason="caption-only frame",
                                     raw_text_observed="PRIVATE caption", presenter_name_raw=None))
            seg = TitleCardSegment(batch_id=batch.id, ordinal=0, segment_label="1", start_ms=0, end_ms_exclusive=15000,
                                   raw_ocr_name="Synthetic Presenter Gamma", session_date_text="February 7, 2025", ready_row_count=2)
            db.add(seg)
            db.flush()
            db.add(ApprovalDecision(batch_id=batch.id, segment_id=seg.id, approval_status="approved",
                                    approved_public_speaker_label="Synthetic Presenter Gamma", rationale="PRIVATE rationale"))
            db.add(EvidenceOverride(batch_id=batch.id, timestamp_ms=10000, decision="include", raw_ocr_caveat="PRIVATE caveat"))
            db.add(AuditEvent(event_type="authoring.test", subject_type="ocr_batch", subject_id=batch.id,
                              actor_json={"user": "seed"}, payload_json={"note": "seed"}))

            # Role grants keyed by project_key.
            grant_project_role(db, user_id=users["reviewer"], project_key=project_key, role="reviewer")
            grant_project_role(db, user_id=users["auditor"], project_key=project_key, role="auditor")
            grant_project_role(db, user_id=users["operator"], project_key=project_key, role="operator")
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

    def test_requires_auth(self):
        for path in ("/api/v1/authoring/batches", f"/api/v1/authoring/batches/{self.batch_id}/observations", "/api/v1/authoring/audit"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401, f"{path}: {r.status_code}")

    def test_member_read_rbac(self):
        # reviewer + auditor are members -> 200; no-role -> 403.
        for who, code in (("reviewer", 200), ("auditor", 200), ("none", 403)):
            r = self.client.get("/api/v1/authoring/batches", headers=self._auth(who))
            self.assertEqual(r.status_code, code, f"batches/{who}: {r.text}")
            r = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth(who))
            self.assertEqual(r.status_code, code, f"observations/{who}: {r.text}")

    def test_audit_read_rbac(self):
        # auditor + operator allowed; reviewer denied (not an oversight role); no-role denied.
        for who, code in (("auditor", 200), ("operator", 200), ("reviewer", 403), ("none", 403)):
            r = self.client.get("/api/v1/authoring/audit", headers=self._auth(who))
            self.assertEqual(r.status_code, code, f"audit/{who}: {r.text}")

    def test_auditor_is_read_only_but_can_read_all_views(self):
        for path in ("batches", f"batches/{self.batch_id}", f"batches/{self.batch_id}/observations",
                     f"batches/{self.batch_id}/segments", f"batches/{self.batch_id}/approvals",
                     f"batches/{self.batch_id}/readiness"):
            r = self.client.get(f"/api/v1/authoring/{path}", headers=self._auth("auditor"))
            self.assertEqual(r.status_code, 200, f"auditor {path}: {r.text}")

    def test_private_fields_only_under_private_block(self):
        r = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()
        self.assertEqual(len(rows), 3)
        for row in rows:
            for name in PRIVATE_FIELD_NAMES:
                self.assertNotIn(name, row, f"private field {name} leaked to top level")
            self.assertIn("private", row)
            self.assertIn("raw_text_observed", row["private"])
        # The private OCR text is present in the private block.
        self.assertTrue(any(row["private"]["raw_text_observed"] for row in rows))

    def test_will_publish_as_in_http_response(self):
        r = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/observations", headers=self._auth("reviewer"))
        rows = {row["timestamp_ms"]: row for row in r.json()}
        self.assertEqual(rows[0]["will_publish_as"], "named")
        self.assertEqual(rows[5000]["will_publish_as"], "dated_speakerless")
        self.assertEqual(rows[5000]["review_state"], "approved")
        self.assertEqual(rows[10000]["will_publish_as"], "unavailable")
        # t=10000 has a seeded evidence override, so "restored" takes precedence
        # over suppression — and it still will_publish_as unavailable (no date).
        self.assertEqual(rows[10000]["review_state"], "restored")

    def test_batch_detail_and_readiness(self):
        r = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}", headers=self._auth("reviewer"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["project_key"], "video-test-sample")  # project_slug was null
        self.assertEqual(body["counts"]["observations"], 3)
        rr = self.client.get(f"/api/v1/authoring/batches/{self.batch_id}/readiness", headers=self._auth("reviewer"))
        self.assertEqual(rr.status_code, 200, rr.text)
        self.assertIn("will_publish_as", rr.json()["summary"])

    # --------------------------------------------------------------------- #
    # Admin project-role grants (platform-admin only)
    # --------------------------------------------------------------------- #
    ADMIN_PROJECT_KEY = "video-admin-test"  # isolated scope; not the seeded one

    def _audit_count(self):
        from sqlalchemy import func, select
        from app.db.session import SessionLocal
        from app.models.entities import AuditEvent
        with SessionLocal() as db:
            return db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

    def _role_rows(self, user_key, role):
        from sqlalchemy import select
        from app.db.session import SessionLocal
        from app.models.authoring_lane import ProjectRole
        with SessionLocal() as db:
            return db.execute(select(ProjectRole).where(
                ProjectRole.user_id == self.user_ids[user_key],
                ProjectRole.project_key == self.ADMIN_PROJECT_KEY,
                ProjectRole.role == role,
            )).scalars().all()

    def _grant_body(self, role, user_key="target"):
        return {"user_id": str(self.user_ids[user_key]), "project_key": self.ADMIN_PROJECT_KEY, "role": role}

    def _revoke(self, role, user_key="target"):
        return self.client.post("/api/v1/authoring/admin/project-roles/revoke",
                                json=self._grant_body(role, user_key), headers=self._auth("admin"))

    def test_admin_can_grant_and_revoke(self):
        before = self._audit_count()
        r = self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("reviewer"), headers=self._auth("admin"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["role"], "reviewer")
        self.assertEqual(len(self._role_rows("target", "reviewer")), 1)
        self.assertEqual(self._audit_count(), before + 1, "exactly one audit on grant")

        rb = self._revoke("reviewer")
        self.assertEqual(rb.status_code, 204, rb.text)
        self.assertEqual(len(self._role_rows("target", "reviewer")), 0)
        self.assertEqual(self._audit_count(), before + 2, "exactly one audit on revoke")

    def test_grant_is_idempotent_without_second_audit(self):
        try:
            r1 = self.client.post("/api/v1/authoring/admin/project-roles",
                                 json=self._grant_body("operator"), headers=self._auth("admin"))
            self.assertEqual(r1.status_code, 200, r1.text)
            mid = self._audit_count()
            r2 = self.client.post("/api/v1/authoring/admin/project-roles",
                                 json=self._grant_body("operator"), headers=self._auth("admin"))
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertEqual(r1.json()["id"], r2.json()["id"], "same grant returned")
            self.assertEqual(self._audit_count(), mid, "no second audit on idempotent re-grant")
            self.assertEqual(len(self._role_rows("target", "operator")), 1)
        finally:
            self._revoke("operator")

    def test_non_admin_cannot_grant_or_revoke(self):
        before = self._audit_count()
        body = self._grant_body("reviewer")
        # Unauthenticated.
        self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles", json=body).status_code, 401)
        # Ordinary member / auditor / operator (project roles) are NOT platform admins.
        for who in ("reviewer", "auditor", "operator", "target", "none"):
            g = self.client.post("/api/v1/authoring/admin/project-roles", json=body, headers=self._auth(who))
            self.assertEqual(g.status_code, 403, f"grant {who}: {g.text}")
            rv = self.client.post("/api/v1/authoring/admin/project-roles/revoke", json=body, headers=self._auth(who))
            self.assertEqual(rv.status_code, 403, f"revoke {who}: {rv.text}")
        self.assertEqual(self._audit_count(), before, "no audit on denied attempts")

    def test_auditor_exclusivity_enforced_both_directions(self):
        try:
            # auditor already held -> granting a mutating role is refused.
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("auditor"), headers=self._auth("admin")).status_code, 200)
            before = self._audit_count()
            r = self.client.post("/api/v1/authoring/admin/project-roles",
                                json=self._grant_body("reviewer"), headers=self._auth("admin"))
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(self._audit_count(), before, "no audit on exclusivity refusal")
            self._revoke("auditor")
            # mutating role held -> granting auditor is refused.
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("approver"), headers=self._auth("admin")).status_code, 200)
            r2 = self.client.post("/api/v1/authoring/admin/project-roles",
                                 json=self._grant_body("auditor"), headers=self._auth("admin"))
            self.assertEqual(r2.status_code, 409, r2.text)
        finally:
            self._revoke("auditor")
            self._revoke("approver")
            self._revoke("reviewer")

    def test_invalid_role_rejected_without_audit(self):
        before = self._audit_count()
        r = self.client.post("/api/v1/authoring/admin/project-roles",
                             json={"user_id": str(self.user_ids["target"]),
                                   "project_key": self.ADMIN_PROJECT_KEY, "role": "superuser"},
                             headers=self._auth("admin"))
        self.assertIn(r.status_code, (400, 422), r.text)  # schema Literal rejects -> 422
        self.assertEqual(self._audit_count(), before, "no audit on invalid role")

    def test_revoke_missing_grant_404_without_audit(self):
        before = self._audit_count()
        r = self._revoke("pm")  # never granted
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self._audit_count(), before, "no audit on missing-grant revoke")

    def test_auditor_and_mutating_role_never_coexist_in_db(self):
        # Drive both orderings through the admin endpoints, then assert the DB
        # never holds auditor + a mutating role for the same (user, project).
        from sqlalchemy import select
        from app.db.session import SessionLocal
        from app.models.authoring_lane import ProjectRole

        def roles_in_db():
            with SessionLocal() as db:
                return set(db.execute(select(ProjectRole.role).where(
                    ProjectRole.user_id == self.user_ids["target"],
                    ProjectRole.project_key == self.ADMIN_PROJECT_KEY,
                )).scalars().all())

        try:
            # auditor first, then reviewer (refused).
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("auditor"), headers=self._auth("admin")).status_code, 200)
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("reviewer"), headers=self._auth("admin")).status_code, 409)
            self.assertEqual(roles_in_db(), {"auditor"}, "reviewer must not have been added alongside auditor")
            self._revoke("auditor")
            # mutating first, then auditor (refused).
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("approver"), headers=self._auth("admin")).status_code, 200)
            self.assertEqual(self.client.post("/api/v1/authoring/admin/project-roles",
                             json=self._grant_body("auditor"), headers=self._auth("admin")).status_code, 409)
            self.assertNotIn("auditor", roles_in_db())
            self.assertEqual(roles_in_db(), {"approver"})
        finally:
            self._revoke("auditor")
            self._revoke("approver")
            self._revoke("reviewer")

    def test_role_scope_advisory_lock_serializes_same_scope(self):
        # Transaction-level proof: while one connection holds the (user, project)
        # advisory lock, a second connection cannot acquire the SAME scope's lock
        # but CAN acquire a different scope's lock. This is the primitive that
        # serializes the exclusivity read-check-insert across concurrent admins.
        from sqlalchemy import text
        from app.db.session import SessionLocal
        from app.api.v1.endpoints.authoring.rbac import role_scope_lock_key

        if SessionLocal().bind.dialect.name != "postgresql":
            self.skipTest("advisory-lock serialization is Postgres-only")

        key_a = role_scope_lock_key(self.user_ids["target"], self.ADMIN_PROJECT_KEY)
        key_b = role_scope_lock_key(self.user_ids["target"], self.ADMIN_PROJECT_KEY + "-other")
        holder = SessionLocal()
        contender = SessionLocal()
        try:
            holder.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key_a})
            same = contender.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key_a}).scalar_one()
            self.assertFalse(same, "second admin must not acquire the same (user, project) scope lock")
            other = contender.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key_b}).scalar_one()
            self.assertTrue(other, "a different scope must remain independently lockable")
        finally:
            holder.rollback(); holder.close()
            contender.rollback(); contender.close()


if __name__ == "__main__":
    unittest.main()
