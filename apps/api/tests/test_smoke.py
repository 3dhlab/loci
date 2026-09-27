import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from redis import Redis
from fastapi.testclient import TestClient
from psycopg import sql


def _minimal_glb_v2(label: str) -> bytes:
    """Build a structurally valid glTF 2 binary with one padded JSON chunk."""
    document = json.dumps(
        {"asset": {"version": "2.0", "generator": f"semantic-smoke-{label}"}},
        separators=(",", ":"),
    ).encode("utf-8")
    json_chunk = document + (b" " * (-len(document) % 4))
    total_length = 12 + 8 + len(json_chunk)
    return (
        b"glTF"
        + (2).to_bytes(4, "little")
        + total_length.to_bytes(4, "little")
        + len(json_chunk).to_bytes(4, "little")
        + b"JSON"
        + json_chunk
    )


def _replace_database_name(database_url: str, database_name: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", parts.query, parts.fragment))


def _psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


BASE_DATABASE_URL = os.environ.get(
    "SEMANTIC_TEST_BASE_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://semantic:semantic@postgres:5432/semantic"),
)
TEST_DATABASE_NAME = os.environ.get("SEMANTIC_TEST_DB", "semantic_smoke")
TEST_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, TEST_DATABASE_NAME)
ADMIN_DATABASE_URL = _replace_database_name(BASE_DATABASE_URL, os.environ.get("SEMANTIC_TEST_ADMIN_DB", "postgres"))
TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="semantic-smoke-media-")

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["MEDIA_ROOT"] = TEST_MEDIA_ROOT
os.environ.setdefault("DISK_ALERT_THRESHOLD_PERCENT", "100")
os.environ.setdefault("JWT_SECRET_KEY", "semantic-smoke-secret")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "semantic-smoke-encryption-key")
os.environ.setdefault("POSTMARK_SERVER_TOKEN", "test-postmark-token")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EMBEDDING_WARMUP_ENABLED", "false")

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.db.base import Base  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.entities import (  # noqa: E402
    AnnotationReviewStatus,
    AuditEvent,
    Clip,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    PublicationManifest,
    Project,
    SearchQueryLog,
    SearchResultFeedback,
    Segment,
    TitleCardObservation,
    TranscriptWindow,
    User,
    VisualWindowDescription,
)
import app.scripts.import_public_projection as import_public_projection_module  # noqa: E402
import app.services.public_sync as public_sync_module  # noqa: E402
from app.scripts.export_public_projection import build_projection  # noqa: E402
from app.schemas.embed import PublicObjectManifestV1  # noqa: E402
from app.schemas.public import PublicEvidencePageResponse  # noqa: E402
from app.services.ai import AIProviderError  # noqa: E402
from app.services.ai.openai_provider import OpenAIProvider  # noqa: E402
from app.services.media_clips import clip_output_path, clip_poster_path, clip_transcript_path  # noqa: E402
from app.services.media_transcode import transcode_output_path  # noqa: E402
from app.worker.jobs import index_transcript_job  # noqa: E402


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._recreate_database()
        cls._create_schema()
        cls._clear_testclient_rate_limits()

        cls.enqueue_transcode_patcher = patch(
            "app.api.v1.endpoints.videos.enqueue_transcode", return_value="job-transcode-smoke"
        )
        cls.enqueue_transcription_patcher = patch(
            "app.api.v1.endpoints.videos.enqueue_transcription", return_value=None
        )
        cls.enqueue_indexing_patcher = patch(
            "app.api.v1.endpoints.transcripts.enqueue_indexing", return_value="job-index-smoke"
        )
        cls.enqueue_clip_export_patcher = patch(
            "app.api.v1.endpoints.clips.enqueue_clip_export", return_value="job-clip-smoke"
        )
        cls.enqueue_transcode_patcher.start()
        cls.enqueue_transcription_patcher.start()
        cls.enqueue_indexing_patcher.start()
        cls.enqueue_clip_export_patcher.start()

        cls.client = TestClient(app)
        cls.user_email = "operator@example.com"
        cls.user_password = "semantic-smoke-pass"

        register_response = cls.client.post(
            "/api/v1/auth/register",
            json={"email": cls.user_email, "password": cls.user_password},
        )
        assert register_response.status_code == 200, register_response.text

        login_response = cls.client.post(
            "/api/v1/auth/login",
            json={"email": cls.user_email, "password": cls.user_password},
        )
        assert login_response.status_code == 200, login_response.text
        cls.token = login_response.json()["access_token"]
        cls.headers = {"Authorization": f"Bearer {cls.token}"}

    def setUp(self):
        self._clear_testclient_rate_limits()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.enqueue_transcode_patcher.stop()
        cls.enqueue_transcription_patcher.stop()
        cls.enqueue_indexing_patcher.stop()
        cls.enqueue_clip_export_patcher.stop()
        engine.dispose()
        cls._drop_database()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    @classmethod
    def _admin_connection(cls):
        return psycopg.connect(_psycopg_url(ADMIN_DATABASE_URL), autocommit=True)

    @classmethod
    def _recreate_database(cls):
        with cls._admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    (TEST_DATABASE_NAME,),
                )
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE_NAME,))
                if cur.fetchone():
                    cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    @classmethod
    def _drop_database(cls):
        with cls._admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    (TEST_DATABASE_NAME,),
                )
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE_NAME,))
                if cur.fetchone():
                    cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    @classmethod
    def _create_schema(cls):
        with psycopg.connect(_psycopg_url(TEST_DATABASE_URL), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        Base.metadata.create_all(bind=engine)

    @classmethod
    def _clear_testclient_rate_limits(cls):
        redis_conn = Redis.from_url(settings.redis_url)
        for prefix in (
            "private_v1_ratelimit",
            "login_ratelimit",
            "public_ratelimit",
            "subscribe_ratelimit",
            "sentry_tunnel_ratelimit",
        ):
            redis_conn.delete(f"{prefix}:testclient")

    def _create_project(self, name: str) -> dict:
        response = self.client.post(
            "/api/v1/projects",
            json={"name": name, "description": "Phase 4A smoke coverage"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _create_object(self, project_id: str, name: str) -> dict:
        response = self.client.post(
            "/api/v1/objects",
            json={
                "project_id": project_id,
                "name": name,
                "description": "Annotated object",
                "external_url": "https://collections.example.org/objects/smoke",
                "metadata_json": {"collection": "smoke"},
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _upload_media(self, filename: str) -> dict:
        response = self.client.post(
            "/api/v1/videos/media-files",
            files={"media_file": (filename, b"synthetic mp4 bytes", "video/mp4")},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _create_video(self, project_id: str, object_id: str, source_path: str, stable_video_id: str, title: str) -> dict:
        sha256_checksum = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
        response = self.client.post(
            "/api/v1/videos",
            json={
                "project_id": project_id,
                "object_id": object_id,
                "stable_video_id": stable_video_id,
                "title": title,
                "original_filename": Path(source_path).name,
                "source_path": source_path,
                "sha256_checksum": sha256_checksum,
                "duration_ms": 12000,
                "enqueue_transcode": True,
                "auto_transcribe_if_missing": False,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _ingest_transcript(self, video_id: str, raw_text: str) -> dict:
        response = self.client.post(
            "/api/v1/transcripts",
            json={
                "video_id": video_id,
                "title": "Smoke Transcript",
                "format": "SRT",
                "language": "en",
                "raw_text": raw_text,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _mark_video_ready(self, video_id: str) -> None:
        response = self.client.patch(
            f"/api/v1/videos/{video_id}",
            json={"status": "READY"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)

        playback_path = transcode_output_path(video_id)
        playback_path.parent.mkdir(parents=True, exist_ok=True)
        playback_path.write_bytes(b"normalized smoke mp4 bytes")

    def _create_poster_file(self, website_object_id: str) -> Path:
        poster_path = Path(TEST_MEDIA_ROOT) / "embed-posters" / f"{website_object_id}.svg"
        poster_path.parent.mkdir(parents=True, exist_ok=True)
        poster_path.write_text(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"400\" height=\"225\"><rect width=\"400\" height=\"225\" fill=\"#102a43\"/></svg>",
            encoding="utf-8",
        )
        return poster_path

    def _create_clip_assets(self, clip_id: str) -> dict:
        clip_path = clip_output_path(clip_id)
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"smoke clip mp4 bytes")

        poster_path = clip_poster_path(clip_id)
        poster_path.write_bytes(b"smoke clip jpg bytes")

        transcript_path = clip_transcript_path(clip_id)
        transcript_path.write_text("public clip transcript excerpt", encoding="utf-8")

        return {
            "clip_path": clip_path,
            "poster_path": poster_path,
            "transcript_path": transcript_path,
        }

    def _create_public_clip(self, video_id: str, website_clip_id: str, *, start_ms: int = 0, end_ms: int = 2000) -> dict:
        response = self.client.post(
            "/api/v1/clips",
            json={
                "video_id": video_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)

        payload = response.json()["clip"]
        assets = self._create_clip_assets(payload["id"])

        with SessionLocal() as db:
            clip = db.get(Clip, payload["id"])
            self.assertIsNotNone(clip)
            clip.output_mp4_path = str(assets["clip_path"])
            clip.metadata_json_path = str(assets["transcript_path"])
            clip.website_clip_id = website_clip_id
            clip.transcript_excerpt = "public clip transcript excerpt"
            clip.citation_text = f"{website_clip_id} citation"
            clip.status = "COMPLETE"
            db.add(clip)
            db.commit()

        return {
            "id": payload["id"],
            "website_clip_id": website_clip_id,
            **assets,
        }

    def _create_title_card_observation(
        self,
        *,
        video_id: str,
        timestamp_ms: int,
        title_card_visible: bool = True,
        presenter_name: str | None = None,
        public_speaker_label: str | None = None,
        session_date_value: date | None = None,
        session_date_text: str | None = None,
        presenter_confidence: str | None = "high",
        session_date_confidence: str | None = "high",
        raw_text_observed: str = "Observed title-card text",
        raw_payload_json: dict | None = None,
        prompt_version: str = "2026-04-24-v1",
        model_provider: str = "openai",
        model_name: str = "gpt-4o-mini",
        source_kind: str = "pilot_artifact",
        source_version: str = "2026-04-24-v1",
        status: str = "ready",
    ) -> str:
        with SessionLocal() as db:
            observation = TitleCardObservation(
                video_id=video_id,
                timestamp_ms=timestamp_ms,
                title_card_visible=title_card_visible,
                presenter_name=presenter_name,
                public_speaker_label=public_speaker_label,
                session_date=session_date_value,
                session_date_text=session_date_text,
                raw_text_observed=raw_text_observed,
                raw_payload_json=raw_payload_json
                or {
                    "presenter_name": presenter_name,
                    "session_date_text": session_date_text,
                },
                confidence_object="none",
                confidence_presenter=presenter_confidence or "none",
                confidence_session_date=session_date_confidence or "none",
                prompt_version=prompt_version,
                model_provider=model_provider,
                model_name=model_name,
                detail="high",
                source_kind=source_kind,
                source_version=source_version,
                status=status,
            )
            db.add(observation)
            db.commit()
            return str(observation.id)

    def _set_embed_fields(
        self,
        *,
        project_id: str,
        object_id: str,
        annotation_ids: list[str],
        clip_ids: list[str] | None,
        website_object_id: str,
        embed_ready: bool,
        published: bool,
        poster_path: Path | None,
    ) -> None:
        with SessionLocal() as db:
            project = db.query(Project).filter_by(id=project_id).one()
            object_row = db.query(Object).filter_by(id=object_id).one()
            model = db.query(ObjectModel).filter_by(object_id=object_id).one()
            annotations = (
                db.query(ObjectModelAnnotation)
                .filter(ObjectModelAnnotation.id.in_(annotation_ids))
                .order_by(ObjectModelAnnotation.created_at.asc())
                .all()
            )
            clips = (
                db.query(Clip)
                .filter(Clip.id.in_(clip_ids or []))
                .order_by(Clip.created_at.asc())
                .all()
            ) if clip_ids else []

            website_project_slug = f"project-{website_object_id}"

            project.website_slug = website_project_slug
            object_row.website_object_id = website_object_id
            object_row.is_embed_ready = embed_ready
            object_row.is_published = published
            model.public_poster_path = str(poster_path) if poster_path is not None else None

            relation_payloads = [
                {
                    "publication_ids": ["demo-publication"],
                    "project_ids": [website_project_slug],
                    "location_ids": ["loc-demo-site"],
                },
                {
                    "publication_ids": ["demo-methods"],
                    "project_ids": [website_project_slug],
                    "location_ids": ["loc-demo-site"],
                },
            ]

            for index, annotation in enumerate(annotations, start=1):
                related_clip = clips[min(index - 1, len(clips) - 1)] if clips else None
                annotation_suffix = f"{website_object_id}-{index}"
                annotation.website_annotation_id = (
                    f"anno-demo-publication-{annotation_suffix}"
                    if index == 1
                    else f"anno-demo-site-{annotation_suffix}"
                )
                annotation.clip_id = related_clip.id if related_clip is not None else None
                annotation.playlist_json = [
                    {
                        "video_id": str(annotation.video_id),
                        "clip_id": str(related_clip.id) if related_clip is not None else None,
                        "transcript_segment_id": str(annotation.transcript_segment_id) if annotation.transcript_segment_id else None,
                        "label": annotation.title,
                        "start_ms": int(annotation.start_ms),
                        "end_ms": int(annotation.end_ms),
                    }
                ]
                annotation.website_relations_json = relation_payloads[min(index - 1, len(relation_payloads) - 1)]
                db.add(annotation)

            db.add(project)
            db.add(object_row)
            db.add(model)
            db.commit()

    def _publish_embed_candidate(self, *, website_object_id: str, publish_object: bool = True, embed_ready: bool = True, with_public_clip: bool = True) -> dict:
        project = self._create_project(f"Embed Manifest Project {website_object_id}")
        object_row = self._create_object(project["id"], f"Embed Object {website_object_id}")
        media_file = self._upload_media(f"{website_object_id}.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            f"video-{website_object_id}",
            f"Embed Video {website_object_id}",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nobject evidence\n\n2\n00:00:02,000 --> 00:00:04,000\nrelated publication",
        )

        embed_model_bytes = _minimal_glb_v2("embed")
        upload_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model",
            files={"model_file": ("embed.glb", embed_model_bytes, "model/gltf-binary")},
            headers=self.headers,
        )
        self.assertEqual(upload_response.status_code, 201, upload_response.text)

        model_patch = self.client.patch(
            f"/api/v1/objects/{object_row['id']}/model",
            json={
                "default_camera_json": {
                    "position": [1.8, 0.9, 2.4],
                    "target": [0.12, 0.43, -0.21],
                },
                "is_published": True,
            },
            headers=self.headers,
        )
        self.assertEqual(model_patch.status_code, 200, model_patch.text)

        annotation_ids: list[str] = []
        for title in ("Demo Publication", "Demo Site"):
            annotation_response = self.client.post(
                f"/api/v1/objects/{object_row['id']}/model/annotations",
                json={
                    "video_id": video["id"],
                    "title": title,
                    "description": f"{title} annotation body",
                    "point_x": 0.12 if title == "Demo Publication" else -0.32,
                    "point_y": 0.43 if title == "Demo Publication" else 0.12,
                    "point_z": -0.21 if title == "Demo Publication" else 0.28,
                    "start_ms": 0,
                    "end_ms": 2000,
                },
                headers=self.headers,
            )
            self.assertEqual(annotation_response.status_code, 201, annotation_response.text)
            annotation = annotation_response.json()
            annotation_ids.append(annotation["id"])

            publish_annotation = self.client.patch(
                f"/api/v1/objects/model-annotations/{annotation['id']}",
                json={"is_published": True},
                headers=self.headers,
            )
            self.assertEqual(publish_annotation.status_code, 200, publish_annotation.text)

        self._mark_video_ready(video["id"])

        clips = []
        if with_public_clip:
            clips.append(
                self._create_public_clip(
                    video["id"],
                    website_clip_id=f"clip-{website_object_id}-1",
                )
            )

        for path in (
            f"/api/v1/objects/{object_row['id']}",
            f"/api/v1/videos/{video['id']}",
            f"/api/v1/transcripts/{transcript_payload['transcript']['id']}",
        ):
            response = self.client.patch(path, json={"is_published": publish_object}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)

        poster_path = self._create_poster_file(website_object_id)
        self._set_embed_fields(
            project_id=project["id"],
            object_id=object_row["id"],
            annotation_ids=annotation_ids,
            clip_ids=[clip["id"] for clip in clips],
            website_object_id=website_object_id,
            embed_ready=embed_ready,
            published=publish_object,
            poster_path=poster_path,
        )

        return {
            "project": project,
            "object": object_row,
            "website_object_id": website_object_id,
            "video": video,
            "transcript": transcript_payload["transcript"],
            "annotation_ids": annotation_ids,
            "clips": clips,
            "poster_path": poster_path,
        }

    def _public_annotation_ids(self, annotation_ids: list[str]) -> list[str]:
        with SessionLocal() as db:
            annotations = (
                db.query(ObjectModelAnnotation)
                .filter(ObjectModelAnnotation.id.in_(annotation_ids))
                .order_by(ObjectModelAnnotation.created_at.asc(), ObjectModelAnnotation.id.asc())
                .all()
            )
            return [annotation.website_annotation_id for annotation in annotations if annotation.website_annotation_id]

    def _public_sync_config(self) -> public_sync_module.PublicSyncConfig:
        fake_key = Path(TEST_MEDIA_ROOT) / "fake-public-sync-key"
        fake_key.write_text("fake-public-sync-key", encoding="utf-8")
        return public_sync_module.PublicSyncConfig(
            remote="semantic@example.test",
            ssh_key=fake_key,
            remote_app_dir="/srv/semantic",
            remote_import_root="/var/lib/semantic/import",
            remote_media_root="/var/lib/semantic/media",
            manifest_basename="public_projection.json",
            media_list_basename="public_media_files.txt",
        )

    def _public_sync_remote_state(self) -> dict:
        manifest, media_paths = build_projection()
        return {
            "projection_digest": public_sync_module._stable_projection_digest(manifest),
            "media_inventory": public_sync_module._build_media_inventory(sorted(set(media_paths))),
        }

    def _capture_public_sync_state(self, captured_state: dict):
        def _fake_scp(config, local_path, remote_dir, *, timeout=900):
            path = Path(local_path)
            if path.name == config.sync_state_basename:
                captured_state.clear()
                captured_state.update(json.loads(path.read_text(encoding="utf-8")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return _fake_scp

    def _seed_visual_window_for_transcript(self, *, transcript_id: str, video_id: str, slug: str) -> dict:
        thumbnail_path = Path(TEST_MEDIA_ROOT) / f"{slug}-thumb.jpg"
        sample_frame_path = Path(TEST_MEDIA_ROOT) / f"{slug}-sample-0.jpg"
        thumbnail_path.write_bytes(b"jpeg-bytes")
        sample_frame_path.write_bytes(b"jpeg-bytes")

        with SessionLocal() as db:
            segments = (
                db.query(Segment)
                .filter(Segment.transcript_id == transcript_id)
                .order_by(Segment.position.asc())
                .all()
            )
            self.assertTrue(segments)

            window = TranscriptWindow(
                transcript_id=transcript_id,
                window_index=0,
                start_position=segments[0].position,
                end_position=segments[-1].position,
                start_ms=segments[0].start_ms,
                end_ms=segments[-1].end_ms,
                text=" ".join(segment.text for segment in segments),
                embedding_vector=None,
            )
            db.add(window)
            db.flush()

            visual_row = VisualWindowDescription(
                transcript_window_id=window.id,
                transcript_id=transcript_id,
                video_id=video_id,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                description_text="Projection fixture visual context.",
                embedding_vector=None,
                thumbnail_path=str(thumbnail_path),
                frame_manifest_json={
                    "thumbnail": {
                        "path": str(thumbnail_path),
                        "start_ms": window.start_ms,
                        "end_ms": window.end_ms,
                    },
                    "sample_frames": [
                        {
                            "path": str(sample_frame_path),
                            "start_ms": window.start_ms,
                            "end_ms": window.end_ms,
                            "sample_index": 0,
                        }
                    ],
                },
                generator_provider="openai",
                generator_model="fixture-vision-model",
                embedding_provider="local",
                embedding_model="fixture-embedding-model",
                status="ready",
            )
            db.add(visual_row)
            db.commit()
            db.refresh(window)
            db.refresh(visual_row)

            return {
                "window_id": str(window.id),
                "visual_id": str(visual_row.id),
                "thumbnail_path": str(thumbnail_path),
                "sample_frame_path": str(sample_frame_path),
            }

    def test_health_endpoint_reports_runtime_dependencies(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["services"]["database"]["status"], "ok")
        self.assertEqual(payload["services"]["redis"]["status"], "ok")
        self.assertEqual(payload["services"]["media_root"]["status"], "ok")
        self.assertTrue(payload["metrics"]["media_root_exists"])
        self.assertIsInstance(payload["metrics"]["queue_depth"], int)
        self.assertIsInstance(payload["metrics"]["failed_jobs"], int)
        self.assertIsInstance(payload["metrics"]["worker_count"], int)
        self.assertIn("transcode", payload["metrics"]["queue_depths"])

    def test_health_endpoint_returns_503_when_runtime_health_degraded(self):
        degraded_snapshot = {
            "status": "degraded",
            "app": {"name": "Semantic API", "environment": "production"},
            "services": {
                "database": {"status": "degraded", "detail": "Database probe failed."},
                "redis": {"status": "ok", "detail": "Redis ping succeeded."},
                "media_root": {"status": "ok", "detail": "Media root is available."},
            },
            "metrics": {
                "queue_depth": 0,
                "failed_jobs": 0,
                "worker_count": 0,
                "disk_usage_percent": 0,
                "disk_alert_threshold_percent": 80,
                "disk_alert_triggered": False,
                "media_root_exists": True,
                "queue_depths": {},
                "failed_job_counts": {},
            },
        }

        with patch("app.main.build_runtime_health_snapshot", return_value=degraded_snapshot):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["status"], "degraded")

    def test_metrics_endpoint_reports_runtime_metrics(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertIn(payload["status"], {"ok", "degraded"})
        self.assertIsInstance(payload["queue_depth"], int)
        self.assertIsInstance(payload["failed_jobs"], int)
        self.assertIsInstance(payload["worker_count"], int)
        self.assertIsInstance(payload["disk_usage_percent"], int)
        self.assertIn("redis", payload["services"])

    def test_login_rate_limiting_blocks_sixth_attempt_from_same_forwarded_ip(self):
        client_ip = "203.0.113.10"
        redis_conn = Redis.from_url(settings.redis_url)
        rate_limit_key = f"login_ratelimit:{client_ip}"
        redis_conn.delete(rate_limit_key)

        try:
            for _ in range(5):
                response = self.client.post(
                    "/api/v1/auth/login",
                    json={"email": self.user_email, "password": "wrong-password"},
                    headers={"X-Forwarded-For": client_ip},
                )
                self.assertEqual(response.status_code, 401, response.text)

            response = self.client.post(
                "/api/v1/auth/login",
                json={"email": self.user_email, "password": "wrong-password"},
                headers={"X-Forwarded-For": client_ip},
            )

            self.assertEqual(response.status_code, 429, response.text)
            self.assertRegex(response.headers.get("retry-after", ""), r"^[0-9]+$")
        finally:
            redis_conn.delete(rate_limit_key)

    def test_cors_preflight_uses_explicit_methods_and_headers(self):
        response = self.client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,range",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        allow_methods = response.headers.get("access-control-allow-methods", "")
        allow_headers = response.headers.get("access-control-allow-headers", "").lower()

        self.assertNotEqual(allow_methods, "*")
        self.assertIn("POST", allow_methods)
        self.assertIn("PATCH", allow_methods)

        self.assertNotEqual(allow_headers, "*")
        self.assertIn("authorization", allow_headers)
        self.assertIn("content-type", allow_headers)
        self.assertIn("range", allow_headers)

    def test_auth_and_core_crud_smoke(self):
        me_response = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(me_response.status_code, 200, me_response.text)
        self.assertEqual(me_response.json()["email"], self.user_email)

        project = self._create_project("CRUD Smoke Project")
        object_row = self._create_object(project["id"], "CRUD Smoke Object")
        self.assertEqual(object_row["external_url"], "https://collections.example.org/objects/smoke")
        media_file = self._upload_media("crud-sample.mp4")
        video_payload = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-crud-video",
            "CRUD Smoke Video",
        )
        video = video_payload["video"]

        videos_response = self.client.get("/api/v1/videos", headers=self.headers)
        self.assertEqual(videos_response.status_code, 200, videos_response.text)
        self.assertTrue(any(item["id"] == video["id"] for item in videos_response.json()))

        object_patch_response = self.client.patch(
            f"/api/v1/objects/{object_row['id']}",
            json={
                "external_url": "https://collections.example.org/objects/crud-updated",
                "metadata_json": {"collection": "smoke", "catalog_id": "crud-01"},
            },
            headers=self.headers,
        )
        self.assertEqual(object_patch_response.status_code, 200, object_patch_response.text)
        self.assertEqual(
            object_patch_response.json()["external_url"],
            "https://collections.example.org/objects/crud-updated",
        )
        self.assertEqual(object_patch_response.json()["metadata_json"]["catalog_id"], "crud-01")

        patch_response = self.client.patch(
            f"/api/v1/videos/{video['id']}",
            json={"title": "CRUD Smoke Video Updated"},
            headers=self.headers,
        )
        self.assertEqual(patch_response.status_code, 200, patch_response.text)
        self.assertEqual(patch_response.json()["title"], "CRUD Smoke Video Updated")

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\ncommunity memory\n\n2\n00:00:02,000 --> 00:00:04,000\nritual practice",
        )
        self.assertEqual(transcript_payload["segments_indexed"], 2)
        self.assertEqual(transcript_payload["indexing_job_id"], "job-index-smoke")

        transcript_detail = self.client.get(f"/api/v1/transcripts/videos/{video['id']}", headers=self.headers)
        self.assertEqual(transcript_detail.status_code, 200, transcript_detail.text)
        self.assertEqual(len(transcript_detail.json()["segments"]), 2)

        search_response = self.client.post(
            "/api/v1/search/segments",
            json={"query": "community", "project_id": project["id"], "page": 1, "page_size": 25},
            headers=self.headers,
        )
        self.assertEqual(search_response.status_code, 200, search_response.text)
        self.assertGreaterEqual(len(search_response.json()["results"]), 1)
        self.assertGreaterEqual(search_response.json()["total_results"], 1)
        self.assertEqual(search_response.json()["page"], 1)
        self.assertEqual(search_response.json()["page_size"], 25)
        self.assertIsNotNone(search_response.json()["search_log_id"])

        with SessionLocal() as db:
            query_log = (
                db.query(SearchQueryLog)
                .filter(SearchQueryLog.query_text == "community")
                .order_by(SearchQueryLog.created_at.desc())
                .first()
            )
            self.assertIsNotNone(query_log)
            self.assertEqual(str(query_log.project_id), project["id"])
            self.assertGreaterEqual(query_log.result_count, 1)

        first_result = search_response.json()["results"][0]
        feedback_response = self.client.post(
            "/api/v1/search/feedback",
            json={
                "search_query_log_id": search_response.json()["search_log_id"],
                "segment_id": first_result["segment_id"],
                "is_relevant": True,
                "lexical_match": first_result["lexical_match"],
                "semantic_score": first_result["semantic_score"],
                "rank_score": first_result["rank_score"],
                "rank_position": 1,
            },
            headers=self.headers,
        )
        self.assertEqual(feedback_response.status_code, 200, feedback_response.text)
        self.assertTrue(feedback_response.json()["is_relevant"])

        with SessionLocal() as db:
            feedback_row = (
                db.query(SearchResultFeedback)
                .filter(SearchResultFeedback.search_query_log_id == search_response.json()["search_log_id"])
                .order_by(SearchResultFeedback.created_at.desc())
                .first()
            )
            self.assertIsNotNone(feedback_row)
            self.assertTrue(feedback_row.is_relevant)

        clip_response = self.client.post(
            "/api/v1/clips",
            json={"video_id": video["id"], "start_ms": 0, "end_ms": 1500},
            headers=self.headers,
        )
        self.assertEqual(clip_response.status_code, 201, clip_response.text)
        self.assertEqual(clip_response.json()["export_job_id"], "job-clip-smoke")

        list_clips_response = self.client.get("/api/v1/clips", headers=self.headers)
        self.assertEqual(list_clips_response.status_code, 200, list_clips_response.text)
        self.assertTrue(any(item["id"] == clip_response.json()["clip"]["id"] for item in list_clips_response.json()["clips"]))

    def test_public_object_standfirst_uses_metadata_then_description_fallback(self):
        project = self._create_project("Public Standfirst Project")
        object_row = self._create_object(project["id"], "Public Standfirst Object")

        publish_response = self.client.patch(
            f"/api/v1/objects/{object_row['id']}",
            json={"is_published": True},
            headers=self.headers,
        )
        self.assertEqual(publish_response.status_code, 200, publish_response.text)

        public_objects = self.client.get(f"/api/v1/public/objects?project_id={project['id']}")
        self.assertEqual(public_objects.status_code, 200, public_objects.text)
        self.assertEqual(public_objects.json()[0]["public_standfirst"], "Annotated object")

        public_preview = self.client.get(f"/api/v1/public/objects/{object_row['id']}/preview")
        self.assertEqual(public_preview.status_code, 200, public_preview.text)
        self.assertEqual(public_preview.json()["object"]["public_standfirst"], "Annotated object")

        update_response = self.client.patch(
            f"/api/v1/objects/{object_row['id']}",
            json={
                "metadata_json": {
                    "collection": "smoke",
                    "public_standfirst": "A concise public standfirst for the published object.",
                }
            },
            headers=self.headers,
        )
        self.assertEqual(update_response.status_code, 200, update_response.text)

        public_objects = self.client.get(f"/api/v1/public/objects?project_id={project['id']}")
        self.assertEqual(public_objects.status_code, 200, public_objects.text)
        self.assertEqual(
            public_objects.json()[0]["public_standfirst"],
            "A concise public standfirst for the published object.",
        )

        public_preview = self.client.get(f"/api/v1/public/objects/{object_row['id']}/preview")
        self.assertEqual(public_preview.status_code, 200, public_preview.text)
        self.assertEqual(
            public_preview.json()["object"]["public_standfirst"],
            "A concise public standfirst for the published object.",
        )

    def test_semantic_search_smoke(self):
        project = self._create_project("Semantic Search Project")
        object_row = self._create_object(project["id"], "Semantic Search Object")
        media_file = self._upload_media("semantic-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-semantic-video",
            "Semantic Search Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nbanana boat\n\n2\n00:00:02,000 --> 00:00:04,000\nceramic vessel",
        )

        embedding = [0.0] * settings.embedding_vector_dimensions
        embedding[0] = 1.0
        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            for segment in segments:
                segment.embedding_vector = embedding
            db.add(
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=0,
                    start_position=segments[0].position,
                    end_position=segments[-1].position,
                    start_ms=segments[0].start_ms,
                    end_ms=segments[-1].end_ms,
                    text=" ".join(segment.text for segment in segments),
                    embedding_vector=embedding,
                )
            )
            db.commit()

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.search.get_embedding_provider", return_value=FakeProvider()):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={"query": "threshold trace", "project_id": project["id"], "page": 1, "page_size": 25},
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        results = search_response.json()["results"]
        self.assertGreaterEqual(len(results), 1)
        self.assertIsNotNone(results[0]["semantic_score"])
        self.assertGreaterEqual(search_response.json()["total_results"], 1)

        with SessionLocal() as db:
            query_log = (
                db.query(SearchQueryLog)
                .filter(SearchQueryLog.query_text == "threshold trace")
                .order_by(SearchQueryLog.created_at.desc())
                .first()
            )
            self.assertIsNotNone(query_log)
            self.assertTrue(query_log.semantic_attempted)
            self.assertTrue(query_log.semantic_succeeded)
            self.assertEqual(query_log.semantic_provider, "local")
            self.assertEqual(query_log.semantic_model, "fake-embedding-model")
            self.assertEqual(query_log.retrieval_mode, "window_first")
            self.assertIsNotNone(query_log.top_window_score)
            self.assertIsNotNone(query_log.top_surfaced_segment_score)

    def test_index_transcript_job_creates_visual_window_descriptions(self):
        project = self._create_project("Visual Description Index Project")
        object_row = self._create_object(project["id"], "Visual Description Object")
        media_file = self._upload_media("visual-description-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-visual-description-video",
            "Visual Description Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nceramic vessel on cloth\n\n2\n00:00:02,000 --> 00:00:04,000\nhand rotates the object",
        )

        embedding = [0.0] * settings.embedding_vector_dimensions
        embedding[0] = 1.0

        class FakeEmbeddingProvider:
            provider_name = "local"
            model_name = "fake-local-model"

            def embed_texts(self, texts):
                return SimpleNamespace(
                    vectors=[embedding for _ in texts],
                    model="fake-local-model",
                    total_tokens=4 * len(texts),
                )

        class FakeVisionProvider:
            provider_name = "openai"

            def describe_images(self, image_paths, prompt):
                self.last_prompt = prompt
                self.last_image_paths = image_paths
                return SimpleNamespace(
                    model="fake-vision-model",
                    description_text="A hand turns a ceramic vessel against a dark background.",
                    total_tokens=11,
                )

        def _fake_run(command, capture_output=True, text=True, check=True, timeout=None):
            output_path = Path(command[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-jpeg-bytes")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        fake_vision_provider = FakeVisionProvider()

        with patch("app.worker.jobs.get_embedding_provider", return_value=FakeEmbeddingProvider()), patch(
            "app.worker.jobs.get_local_embedding_provider", return_value=FakeEmbeddingProvider()
        ), patch("app.worker.jobs.get_vision_provider", return_value=fake_vision_provider), patch(
            "app.worker.jobs.subprocess.run", side_effect=_fake_run
        ):
            result = index_transcript_job(transcript_payload["transcript"]["id"])

        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["visual_descriptions_ready"], 1)
        self.assertEqual(result["visual_descriptions_failed"], 0)
        self.assertIn("Transcript excerpt:", fake_vision_provider.last_prompt)
        self.assertIn("ceramic vessel on cloth", fake_vision_provider.last_prompt)
        self.assertIn("Return exactly 2 short sentences", fake_vision_provider.last_prompt)
        self.assertIn("Sentence 1: main visible object and material or handling.", fake_vision_provider.last_prompt)

        with SessionLocal() as db:
            visual_rows = (
                db.query(VisualWindowDescription)
                .filter(VisualWindowDescription.transcript_id == transcript_payload["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc())
                .all()
            )
            self.assertGreaterEqual(len(visual_rows), 1)
            self.assertEqual(visual_rows[0].status, "ready")
            self.assertIsNotNone(visual_rows[0].description_text)
            self.assertIsNotNone(visual_rows[0].embedding_vector)
            self.assertIsNotNone(visual_rows[0].thumbnail_path)
            self.assertTrue(Path(visual_rows[0].thumbnail_path).exists())
            self.assertEqual(len(visual_rows[0].frame_manifest_json["sample_frames"]), 5)
            self.assertEqual(visual_rows[0].frame_manifest_json["prompt_version"], settings.visual_description_prompt_version)

    def test_visual_rows_survive_reindex_when_vision_provider_is_unavailable(self):
        project = self._create_project("Visual Provider Guard Project")
        object_row = self._create_object(project["id"], "Visual Provider Guard Object")
        media_file = self._upload_media("visual-provider-guard-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-visual-provider-guard-video",
            "Visual Provider Guard Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nceramic vessel on cloth\n\n2\n00:00:02,000 --> 00:00:04,000\nhand rotates the object",
        )

        embedding = [0.0] * settings.embedding_vector_dimensions
        embedding[0] = 1.0

        class FakeEmbeddingProvider:
            provider_name = "local"
            model_name = "fake-local-model"

            def embed_texts(self, texts):
                return SimpleNamespace(
                    vectors=[embedding for _ in texts],
                    model="fake-local-model",
                    total_tokens=4 * len(texts),
                )

        class FakeVisionProvider:
            provider_name = "openai"

            def describe_images(self, image_paths, prompt):
                return SimpleNamespace(
                    model="fake-vision-model",
                    description_text="A hand turns a ceramic vessel against a dark background.",
                    total_tokens=11,
                )

        def _fake_run(command, capture_output=True, text=True, check=True, timeout=None):
            output_path = Path(command[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-jpeg-bytes")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("app.worker.jobs.get_embedding_provider", return_value=FakeEmbeddingProvider()), patch(
            "app.worker.jobs.get_local_embedding_provider", return_value=FakeEmbeddingProvider()
        ), patch("app.worker.jobs.get_vision_provider", return_value=FakeVisionProvider()), patch(
            "app.worker.jobs.subprocess.run", side_effect=_fake_run
        ):
            initial_result = index_transcript_job(transcript_payload["transcript"]["id"])

        self.assertEqual(initial_result["visual_description_status"], "ready")

        with SessionLocal() as db:
            initial_rows = (
                db.query(VisualWindowDescription)
                .filter(VisualWindowDescription.transcript_id == transcript_payload["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc())
                .all()
            )
            self.assertGreaterEqual(len(initial_rows), 1)
            initial_window_ids = [row.transcript_window_id for row in initial_rows]
            initial_descriptions = [row.description_text for row in initial_rows]

        with patch("app.worker.jobs.get_embedding_provider", return_value=FakeEmbeddingProvider()), patch(
            "app.worker.jobs.get_local_embedding_provider", return_value=FakeEmbeddingProvider()
        ), patch("app.worker.jobs.get_vision_provider", side_effect=AIProviderError("OPENAI_API_KEY is not configured")):
            rerun_result = index_transcript_job(transcript_payload["transcript"]["id"])

        self.assertEqual(rerun_result["status"], "completed")
        self.assertEqual(rerun_result["visual_description_status"], "provider_unavailable")
        self.assertGreaterEqual(rerun_result["visual_descriptions_ready"], 1)
        self.assertEqual(rerun_result["visual_descriptions_failed"], 0)

        with SessionLocal() as db:
            rerun_rows = (
                db.query(VisualWindowDescription)
                .filter(VisualWindowDescription.transcript_id == transcript_payload["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc())
                .all()
            )

        self.assertEqual([row.transcript_window_id for row in rerun_rows], initial_window_ids)
        self.assertEqual([row.description_text for row in rerun_rows], initial_descriptions)

    def test_visual_only_search_mode_smoke(self):
        project = self._create_project("Visual Search Project")
        object_row = self._create_object(project["id"], "Visual Search Object")
        media_file = self._upload_media("visual-search-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-visual-search-video",
            "Visual Search Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nspoken words here\n\n2\n00:00:02,000 --> 00:00:04,000\nmore spoken words here",
        )

        query_embedding = [0.0] * settings.embedding_vector_dimensions
        query_embedding[0] = 1.0
        stored_embedding = [0.0] * settings.embedding_vector_dimensions
        stored_embedding[0] = 1.0

        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            for segment in segments:
                segment.embedding_vector = stored_embedding
            window = TranscriptWindow(
                transcript_id=transcript_payload["transcript"]["id"],
                window_index=0,
                start_position=segments[0].position,
                end_position=segments[-1].position,
                start_ms=segments[0].start_ms,
                end_ms=segments[-1].end_ms,
                text=" ".join(segment.text for segment in segments),
                embedding_vector=stored_embedding,
            )
            db.add(window)
            db.flush()
            db.add(
                VisualWindowDescription(
                    transcript_window_id=window.id,
                    transcript_id=transcript_payload["transcript"]["id"],
                    video_id=video["id"],
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    description_text="A hand rotates a ceramic vessel.",
                    embedding_vector=stored_embedding,
                    thumbnail_path=str(Path(TEST_MEDIA_ROOT) / "visual-search-thumb.jpg"),
                    frame_manifest_json={"sample_frames": []},
                    generator_provider="openai",
                    generator_model="fake-vision-model",
                    embedding_provider="local",
                    embedding_model="fake-local-model",
                    status="ready",
                )
            )
            db.commit()

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[query_embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.search.get_embedding_provider", return_value=FakeProvider()):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={
                    "query": "ceramic vessel",
                    "project_id": project["id"],
                    "retrieval_mode": "visual_only",
                    "page": 1,
                    "page_size": 25,
                },
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        payload = search_response.json()
        self.assertEqual(payload["retrieval_mode"], "visual_only")
        self.assertGreaterEqual(payload["total_results"], 1)
        self.assertIsNotNone(payload["results"][0]["visual_score"])
        self.assertIsNotNone(payload["results"][0]["transcript_window_id"])
        self.assertIsNotNone(payload["results"][0]["thumbnail_url"])
        self.assertEqual(payload["results"][0]["sample_frames"], [])

    def test_visual_sample_frame_endpoint_supports_zero_index(self):
        project = self._create_project("Visual Frame Endpoint Project")
        object_row = self._create_object(project["id"], "Visual Frame Endpoint Object")
        media_file = self._upload_media("visual-frame-endpoint-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-visual-frame-endpoint-video",
            "Visual Frame Endpoint Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nspoken words here\n\n2\n00:00:02,000 --> 00:00:04,000\nmore spoken words here",
        )

        frame_zero_path = Path(TEST_MEDIA_ROOT) / "visual-frame-endpoint-sample-00.jpg"
        frame_one_path = Path(TEST_MEDIA_ROOT) / "visual-frame-endpoint-sample-01.jpg"
        thumbnail_path = Path(TEST_MEDIA_ROOT) / "visual-frame-endpoint-thumb.jpg"
        frame_zero_path.write_bytes(b"frame-zero-bytes")
        frame_one_path.write_bytes(b"frame-one-bytes")
        thumbnail_path.write_bytes(b"thumb-bytes")

        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            window = TranscriptWindow(
                transcript_id=transcript_payload["transcript"]["id"],
                window_index=0,
                start_position=segments[0].position,
                end_position=segments[-1].position,
                start_ms=segments[0].start_ms,
                end_ms=segments[-1].end_ms,
                text=" ".join(segment.text for segment in segments),
            )
            db.add(window)
            db.flush()
            db.add(
                VisualWindowDescription(
                    transcript_window_id=window.id,
                    transcript_id=transcript_payload["transcript"]["id"],
                    video_id=video["id"],
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    description_text="A hand rotates a ceramic vessel.",
                    thumbnail_path=str(thumbnail_path),
                    frame_manifest_json={
                        "sample_frames": [
                            {"sample_index": 0, "timestamp_ms": 0, "path": str(frame_zero_path)},
                            {"sample_index": 1, "timestamp_ms": 1000, "path": str(frame_one_path)},
                        ]
                    },
                    generator_provider="openai",
                    generator_model="fake-vision-model",
                    embedding_provider="local",
                    embedding_model="fake-local-model",
                    status="ready",
                )
            )
            db.commit()
            transcript_window_id = str(window.id)

        sample_response = self.client.get(
            f"/api/v1/search/visual-windows/{transcript_window_id}/frames/0",
            headers=self.headers,
        )

        self.assertEqual(sample_response.status_code, 200, sample_response.text)
        self.assertEqual(sample_response.headers["content-type"], "image/jpeg")
        self.assertEqual(sample_response.content, b"frame-zero-bytes")

    def test_combined_search_mode_smoke(self):
        project = self._create_project("Combined Search Project")
        object_row = self._create_object(project["id"], "Combined Search Object")
        media_file = self._upload_media("combined-search-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-combined-search-video",
            "Combined Search Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nspoken threshold content\n\n2\n00:00:02,000 --> 00:00:04,000\nmore threshold content",
        )

        query_embedding = [0.0] * settings.embedding_vector_dimensions
        query_embedding[0] = 1.0
        stored_embedding = [0.0] * settings.embedding_vector_dimensions
        stored_embedding[0] = 1.0

        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            for segment in segments:
                segment.embedding_vector = stored_embedding
            window = TranscriptWindow(
                transcript_id=transcript_payload["transcript"]["id"],
                window_index=0,
                start_position=segments[0].position,
                end_position=segments[-1].position,
                start_ms=segments[0].start_ms,
                end_ms=segments[-1].end_ms,
                text=" ".join(segment.text for segment in segments),
                embedding_vector=stored_embedding,
            )
            db.add(window)
            db.flush()
            db.add(
                VisualWindowDescription(
                    transcript_window_id=window.id,
                    transcript_id=transcript_payload["transcript"]["id"],
                    video_id=video["id"],
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    description_text="A person handles the object while speaking.",
                    embedding_vector=stored_embedding,
                    thumbnail_path=str(Path(TEST_MEDIA_ROOT) / "combined-search-thumb.jpg"),
                    frame_manifest_json={"sample_frames": []},
                    generator_provider="openai",
                    generator_model="fake-vision-model",
                    embedding_provider="local",
                    embedding_model="fake-local-model",
                    status="ready",
                )
            )
            db.commit()

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[query_embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.search.get_embedding_provider", return_value=FakeProvider()):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={
                    "query": "threshold content",
                    "project_id": project["id"],
                    "retrieval_mode": "combined",
                    "page": 1,
                    "page_size": 25,
                },
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        payload = search_response.json()
        self.assertEqual(payload["retrieval_mode"], "combined")
        self.assertGreaterEqual(payload["total_results"], 1)
        self.assertIsNotNone(payload["results"][0]["visual_score"])
        self.assertIsNotNone(payload["results"][0]["context_start_ms"])
        self.assertIsNotNone(payload["results"][0]["thumbnail_url"])

    def test_openai_provider_retries_transient_connect_errors_for_image_description(self):
        image_path = Path(TEST_MEDIA_ROOT) / "retry-sample.jpg"
        image_path.write_bytes(b"retry-bytes")

        provider = OpenAIProvider(
            api_key="test-key",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=settings.embedding_vector_dimensions,
            transcription_model="whisper-1",
            vision_model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            timeout_seconds=5,
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "model": "gpt-4o-mini",
                    "choices": [{"message": {"content": "A spoon is held in a hand. The setting shifts to an indoor display."}}],
                    "usage": {"total_tokens": 12},
                }

        class FakeClient:
            attempts = 0

            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None, data=None, files=None):
                FakeClient.attempts += 1
                if FakeClient.attempts < 3:
                    raise httpx.ConnectError("temporary dns failure")
                return FakeResponse()

        with patch("app.services.ai.openai_provider.httpx.Client", FakeClient), patch(
            "app.services.ai.openai_provider.time.sleep"
        ):
            result = provider.describe_images([str(image_path)], "Describe the image.")

        self.assertEqual(FakeClient.attempts, 3)
        self.assertEqual(result.description_text, "A spoon is held in a hand. The setting shifts to an indoor display.")

    def test_semantic_threshold_filters_low_semantic_only_results(self):
        project = self._create_project("Semantic Threshold Project")
        object_row = self._create_object(project["id"], "Semantic Threshold Object")
        media_file = self._upload_media("semantic-threshold-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-semantic-threshold-video",
            "Semantic Threshold Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nbanana boat\n\n2\n00:00:02,000 --> 00:00:04,000\nceramic vessel",
        )

        query_embedding = [0.0] * settings.embedding_vector_dimensions
        query_embedding[0] = 1.0
        stored_embedding = [0.0] * settings.embedding_vector_dimensions
        stored_embedding[1] = 1.0

        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            for segment in segments:
                segment.embedding_vector = stored_embedding
            db.add(
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=0,
                    start_position=segments[0].position,
                    end_position=segments[-1].position,
                    start_ms=segments[0].start_ms,
                    end_ms=segments[-1].end_ms,
                    text=" ".join(segment.text for segment in segments),
                    embedding_vector=stored_embedding,
                )
            )
            db.commit()

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[query_embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.search.get_embedding_provider", return_value=FakeProvider()):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={"query": "memory trace", "project_id": project["id"], "page": 1, "page_size": 25},
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        self.assertEqual(search_response.json()["total_results"], 0)
        self.assertEqual(len(search_response.json()["results"]), 0)

        with SessionLocal() as db:
            query_log = (
                db.query(SearchQueryLog)
                .filter(SearchQueryLog.query_text == "memory trace")
                .order_by(SearchQueryLog.created_at.desc())
                .first()
            )
            self.assertIsNotNone(query_log)
            self.assertTrue(query_log.semantic_attempted)
            self.assertTrue(query_log.semantic_succeeded)
            self.assertEqual(query_log.retrieval_mode, "window_first")
            self.assertIsNotNone(query_log.top_window_score)
            self.assertGreaterEqual(float(query_log.top_window_score), 0.49)
            self.assertLess(float(query_log.top_window_score), 0.68)

    def test_lexical_search_uses_word_boundaries(self):
        project = self._create_project("Lexical Boundary Project")
        object_row = self._create_object(project["id"], "Lexical Boundary Object")
        media_file = self._upload_media("lexical-boundary-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-lexical-boundary-video",
            "Lexical Boundary Video",
        )["video"]

        self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nwhen strangers come to the village\n\n2\n00:00:02,000 --> 00:00:04,000\nanger can rise very quickly",
        )

        with patch("app.api.v1.endpoints.search.get_embedding_provider", side_effect=AIProviderError("semantic disabled")):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={"query": "anger", "project_id": project["id"], "page": 1, "page_size": 25},
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        payload = search_response.json()
        self.assertEqual(payload["total_results"], 1)
        self.assertEqual(len(payload["results"]), 1)
        self.assertIn("anger can rise", payload["results"][0]["text"])
        self.assertTrue(payload["results"][0]["lexical_match"])

    def test_public_search_uses_same_semantic_floor_as_analysis(self):
        project = self._create_project("Public Threshold Project")
        object_row = self._create_object(project["id"], "Public Threshold Object")
        media_file = self._upload_media("public-threshold-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-public-threshold-video",
            "Public Threshold Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nceramic vessel\n\n2\n00:00:02,000 --> 00:00:04,000\nritual memory",
        )

        query_embedding = [0.0] * settings.embedding_vector_dimensions
        query_embedding[0] = 1.0
        stored_embedding = [0.0] * settings.embedding_vector_dimensions
        stored_embedding[0] = 1.0
        stored_embedding[1] = 1.8

        with SessionLocal() as db:
            segments = db.query(Segment).filter(Segment.transcript_id == transcript_payload["transcript"]["id"]).all()
            self.assertGreaterEqual(len(segments), 2)
            for segment in segments:
                segment.embedding_vector = stored_embedding
            db.add(
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=0,
                    start_position=segments[0].position,
                    end_position=segments[-1].position,
                    start_ms=segments[0].start_ms,
                    end_ms=segments[-1].end_ms,
                    text=" ".join(segment.text for segment in segments),
                    embedding_vector=stored_embedding,
                )
            )
            db.commit()

        for path in (
            f"/api/v1/objects/{object_row['id']}",
            f"/api/v1/videos/{video['id']}",
            f"/api/v1/transcripts/{transcript_payload['transcript']['id']}",
        ):
            response = self.client.patch(path, json={"is_published": True}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["is_published"])

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[query_embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.search.get_embedding_provider", return_value=FakeProvider()):
            analysis_search = self.client.post(
                "/api/v1/search/segments",
                json={"query": "threshold band", "project_id": project["id"], "page": 1, "page_size": 25},
                headers=self.headers,
            )
            public_search = self.client.post(
                "/api/v1/search/segments",
                json={"query": "threshold band", "project_id": project["id"], "published_only": True, "page": 1, "page_size": 25},
                headers=self.headers,
            )

        self.assertEqual(analysis_search.status_code, 200, analysis_search.text)
        self.assertEqual(public_search.status_code, 200, public_search.text)
        self.assertEqual(analysis_search.json()["total_results"], 0)
        self.assertEqual(public_search.json()["total_results"], 0)

    def test_live_database_sync_endpoint_smoke(self):
        with patch(
            "app.api.v1.endpoints.ops.sync_public_projection",
            return_value=SimpleNamespace(
                generated_at="2026-03-29T07:00:00Z",
                project_count=2,
                object_count=3,
                video_count=3,
                transcript_count=3,
                annotation_count=9,
                media_file_count=6,
            ),
        ):
            response = self.client.post("/api/v1/ops/live-database/sync", headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object_count"], 3)
        self.assertEqual(payload["video_count"], 3)
        self.assertEqual(payload["annotation_count"], 9)
        self.assertEqual(payload["media_file_count"], 6)
        self.assertIn("Live Database sync complete.", payload["detail"])

    def test_live_database_sync_endpoint_passes_actor_context(self):
        captured: dict[str, dict] = {}

        def _fake_sync_public_projection(*, actor=None, source_release_tag=None, progress_callback=None):
            captured["actor"] = actor
            captured["source_release_tag"] = source_release_tag
            captured["progress_callback"] = progress_callback
            return SimpleNamespace(
                generated_at="2026-03-29T07:00:00Z",
                project_count=1,
                object_count=1,
                video_count=1,
                transcript_count=1,
                annotation_count=1,
                media_file_count=3,
                changed_media_file_count=3,
                manifest_changed=True,
            )

        with patch("app.api.v1.endpoints.ops.sync_public_projection", side_effect=_fake_sync_public_projection):
            response = self.client.post("/api/v1/ops/live-database/sync", headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured["actor"]["type"], "user")
        self.assertEqual(captured["actor"]["email"], self.user_email)
        self.assertEqual(captured["actor"]["trigger"], "ops.live_database.sync")

    def test_object_update_writes_publish_and_metadata_audit_events(self):
        project = self._create_project("Audit Object Project")
        object_row = self._create_object(project["id"], "Audit Object")

        response = self.client.patch(
            f"/api/v1/objects/{object_row['id']}",
            json={
                "description": "Updated description for audit coverage.",
                "metadata_json": {"collection": "audit", "catalog_id": "audit-01"},
                "is_published": True,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)

        with SessionLocal() as db:
            events = (
                db.query(AuditEvent)
                .filter(AuditEvent.subject_type == "object", AuditEvent.subject_id == object_row["id"])
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            event_types = {event.event_type for event in events}
            self.assertIn("object.published", event_types)
            self.assertIn("object.metadata_changed", event_types)
            metadata_event = next(event for event in events if event.event_type == "object.metadata_changed")
            self.assertEqual(metadata_event.actor_json["email"], self.user_email)
            self.assertIn("description", metadata_event.payload_json["changes"])
            self.assertIn("metadata_json", metadata_event.payload_json["changes"])

    def test_transcript_ingest_writes_reindex_audit_event(self):
        project = self._create_project("Audit Transcript Project")
        object_row = self._create_object(project["id"], "Audit Transcript Object")
        media_file = self._upload_media("audit-transcript-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "audit-transcript-video",
            "Audit Transcript Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\naudit transcript line",
        )

        with SessionLocal() as db:
            events = (
                db.query(AuditEvent)
                .filter(AuditEvent.subject_type == "transcript", AuditEvent.subject_id == transcript_payload["transcript"]["id"])
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            self.assertTrue(any(event.event_type == "transcript.reindex_requested" for event in events))
            event = next(event for event in events if event.event_type == "transcript.reindex_requested")
            self.assertEqual(event.actor_json["email"], self.user_email)
            self.assertEqual(event.payload_json["segments_indexed"], 1)
            self.assertEqual(event.payload_json["source"], "MANUAL")

    def test_live_database_sync_status_endpoints_smoke(self):
        running_status = SimpleNamespace(
            operation_id="sync-op-123",
            state="RUNNING",
            stage_key="upload_media_bundle",
            stage_label="Uploading media bundle",
            detail="Uploading 6 published media files to the VPS.",
            progress_percent=72,
            started_at="2026-03-30T10:00:00Z",
            finished_at=None,
            generated_at="2026-03-30T10:00:00Z",
            project_count=2,
            object_count=3,
            video_count=3,
            transcript_count=3,
            annotation_count=9,
            media_file_count=6,
            error=None,
        )
        with patch("app.api.v1.endpoints.ops.begin_live_database_sync", return_value=running_status):
            start_response = self.client.post("/api/v1/ops/live-database/sync/start", headers=self.headers)

        self.assertEqual(start_response.status_code, 202, start_response.text)
        self.assertEqual(start_response.json()["state"], "RUNNING")
        self.assertEqual(start_response.json()["progress_percent"], 72)

        completed_status = SimpleNamespace(
            operation_id="sync-op-123",
            state="SUCCEEDED",
            stage_key="complete",
            stage_label="Sync complete",
            detail="Live Database sync complete. 3 objects, 3 videos, 9 annotations, and 6 media files promoted.",
            progress_percent=100,
            started_at="2026-03-30T10:00:00Z",
            finished_at="2026-03-30T10:01:12Z",
            generated_at="2026-03-30T10:00:00Z",
            project_count=2,
            object_count=3,
            video_count=3,
            transcript_count=3,
            annotation_count=9,
            media_file_count=6,
            error=None,
        )
        with patch("app.api.v1.endpoints.ops.get_live_database_sync_status", return_value=completed_status):
            status_response = self.client.get("/api/v1/ops/live-database/sync/status", headers=self.headers)

        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(status_response.json()["state"], "SUCCEEDED")
        self.assertEqual(status_response.json()["progress_percent"], 100)
        self.assertEqual(status_response.json()["media_file_count"], 6)

    def test_object_model_and_annotation_crud_smoke(self):
        project = self._create_project("3D Smoke Project")
        object_row = self._create_object(project["id"], "3D Smoke Object")
        media_file = self._upload_media("model-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-3d-video",
            "3D Smoke Video",
        )["video"]

        self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nhandle detail\n\n2\n00:00:02,000 --> 00:00:04,000\nsurface mark",
        )
        segment_id = self.client.get(f"/api/v1/transcripts/videos/{video['id']}", headers=self.headers).json()["segments"][0]["id"]

        model_v1_bytes = _minimal_glb_v2("object-model-v1")
        upload_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model",
            files={"model_file": ("artifact.glb", model_v1_bytes, "model/gltf-binary")},
            headers=self.headers,
        )
        self.assertEqual(upload_response.status_code, 201, upload_response.text)
        self.assertEqual(upload_response.json()["revision_number"], 1)

        get_model_response = self.client.get(f"/api/v1/objects/{object_row['id']}/model", headers=self.headers)
        self.assertEqual(get_model_response.status_code, 200, get_model_response.text)

        file_response = self.client.get(
            f"/api/v1/objects/{object_row['id']}/model/file?access_token={self.token}"
        )
        self.assertEqual(file_response.status_code, 200, file_response.text)
        self.assertEqual(file_response.content, model_v1_bytes)

        annotation_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model/annotations",
            json={
                "video_id": video["id"],
                "transcript_segment_id": segment_id,
                "title": "Handle wear",
                "description": "Maps the key interpretive clip to the handle.",
                "point_x": 0.1,
                "point_y": 0.2,
                "point_z": 0.3,
                "normal_x": 0.0,
                "normal_y": 1.0,
                "normal_z": 0.0,
                "start_ms": 0,
                "end_ms": 2000,
                "playlist": [
                    {
                        "video_id": video["id"],
                        "transcript_segment_id": segment_id,
                        "label": "Clip 1",
                        "start_ms": 0,
                        "end_ms": 2000,
                    }
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(annotation_response.status_code, 201, annotation_response.text)
        annotation = annotation_response.json()
        self.assertEqual(annotation["review_status"], AnnotationReviewStatus.ACTIVE.value)

        list_annotations_response = self.client.get(
            f"/api/v1/objects/{object_row['id']}/model/annotations",
            headers=self.headers,
        )
        self.assertEqual(list_annotations_response.status_code, 200, list_annotations_response.text)
        self.assertEqual(len(list_annotations_response.json()), 1)

        model_v2_bytes = _minimal_glb_v2("object-model-v2")
        replace_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model",
            files={"model_file": ("artifact-v2.glb", model_v2_bytes, "model/gltf-binary")},
            headers=self.headers,
        )
        self.assertEqual(replace_response.status_code, 201, replace_response.text)
        self.assertEqual(replace_response.json()["revision_number"], 2)

        review_required_response = self.client.get(
            f"/api/v1/objects/{object_row['id']}/model/annotations",
            headers=self.headers,
        )
        self.assertEqual(review_required_response.status_code, 200, review_required_response.text)
        self.assertEqual(review_required_response.json()[0]["review_status"], AnnotationReviewStatus.REVIEW_REQUIRED.value)

        patch_response = self.client.patch(
            f"/api/v1/objects/model-annotations/{annotation['id']}",
            json={"title": "Handle wear updated"},
            headers=self.headers,
        )
        self.assertEqual(patch_response.status_code, 200, patch_response.text)
        self.assertEqual(patch_response.json()["title"], "Handle wear updated")

        mark_reviewed_response = self.client.post(
            f"/api/v1/objects/model-annotations/{annotation['id']}/mark-reviewed",
            headers=self.headers,
        )
        self.assertEqual(mark_reviewed_response.status_code, 200, mark_reviewed_response.text)
        self.assertEqual(mark_reviewed_response.json()["review_status"], AnnotationReviewStatus.ACTIVE.value)

        delete_annotation_response = self.client.delete(
            f"/api/v1/objects/model-annotations/{annotation['id']}",
            headers=self.headers,
        )
        self.assertEqual(delete_annotation_response.status_code, 204, delete_annotation_response.text)

        delete_model_response = self.client.delete(
            f"/api/v1/objects/{object_row['id']}/model",
            headers=self.headers,
        )
        self.assertEqual(delete_model_response.status_code, 204, delete_model_response.text)

    def test_publication_state_filters_smoke(self):
        project = self._create_project("Publication Smoke Project")
        object_row = self._create_object(project["id"], "Publication Smoke Object")
        media_file = self._upload_media("publication-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-publication-video",
            "Publication Smoke Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\npublic memory\n\n2\n00:00:02,000 --> 00:00:04,000\nobject testimony",
        )
        transcript = transcript_payload["transcript"]
        segment_id = self.client.get(f"/api/v1/transcripts/videos/{video['id']}", headers=self.headers).json()["segments"][0]["id"]

        publication_model_bytes = _minimal_glb_v2("publication")
        upload_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model",
            files={"model_file": ("publication.glb", publication_model_bytes, "model/gltf-binary")},
            headers=self.headers,
        )
        self.assertEqual(upload_response.status_code, 201, upload_response.text)

        annotation_response = self.client.post(
            f"/api/v1/objects/{object_row['id']}/model/annotations",
            json={
                "video_id": video["id"],
                "transcript_segment_id": segment_id,
                "title": "Publication callout",
                "description": "Links the published object to the transcript evidence.",
                "point_x": 0.2,
                "point_y": 0.3,
                "point_z": 0.4,
                "start_ms": 0,
                "end_ms": 2000,
            },
            headers=self.headers,
        )
        self.assertEqual(annotation_response.status_code, 201, annotation_response.text)
        annotation = annotation_response.json()

        unpublished_search = self.client.post(
            "/api/v1/search/segments",
            json={"query": "public memory", "project_id": project["id"], "published_only": True, "page": 1, "page_size": 25},
            headers=self.headers,
        )
        self.assertEqual(unpublished_search.status_code, 200, unpublished_search.text)
        self.assertEqual(unpublished_search.json()["total_results"], 0)

        missing_transcript = self.client.get(
            f"/api/v1/transcripts/videos/{video['id']}?published_only=true",
            headers=self.headers,
        )
        self.assertEqual(missing_transcript.status_code, 404, missing_transcript.text)

        missing_annotations = self.client.get(
            f"/api/v1/objects/{object_row['id']}/model/annotations?published_only=true",
            headers=self.headers,
        )
        self.assertEqual(missing_annotations.status_code, 200, missing_annotations.text)
        self.assertEqual(len(missing_annotations.json()), 0)

        for path in (
            f"/api/v1/objects/{object_row['id']}",
            f"/api/v1/videos/{video['id']}",
            f"/api/v1/transcripts/{transcript['id']}",
            f"/api/v1/objects/{object_row['id']}/model",
            f"/api/v1/objects/model-annotations/{annotation['id']}",
        ):
            response = self.client.patch(path, json={"is_published": True}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["is_published"])

        published_transcript = self.client.get(
            f"/api/v1/transcripts/videos/{video['id']}?published_only=true",
            headers=self.headers,
        )
        self.assertEqual(published_transcript.status_code, 200, published_transcript.text)
        self.assertEqual(published_transcript.json()["transcript"]["id"], transcript["id"])

        published_annotations = self.client.get(
            f"/api/v1/objects/{object_row['id']}/model/annotations?published_only=true",
            headers=self.headers,
        )
        self.assertEqual(published_annotations.status_code, 200, published_annotations.text)
        self.assertEqual(len(published_annotations.json()), 1)
        self.assertEqual(published_annotations.json()[0]["id"], annotation["id"])

        published_search = self.client.post(
            "/api/v1/search/segments",
            json={"query": "public memory", "project_id": project["id"], "published_only": True, "page": 1, "page_size": 25},
            headers=self.headers,
        )
        self.assertEqual(published_search.status_code, 200, published_search.text)
        self.assertGreaterEqual(published_search.json()["total_results"], 1)

        self._mark_video_ready(video["id"])

        public_projects = self.client.get("/api/v1/public/projects")
        self.assertEqual(public_projects.status_code, 200, public_projects.text)
        self.assertTrue(any(item["id"] == project["id"] for item in public_projects.json()))

        public_objects = self.client.get(f"/api/v1/public/objects?project_id={project['id']}")
        self.assertEqual(public_objects.status_code, 200, public_objects.text)
        self.assertEqual(len(public_objects.json()), 1)
        self.assertEqual(public_objects.json()[0]["id"], object_row["id"])

        public_videos = self.client.get(f"/api/v1/public/videos?object_id={object_row['id']}")
        self.assertEqual(public_videos.status_code, 200, public_videos.text)
        self.assertEqual(len(public_videos.json()), 1)
        self.assertEqual(public_videos.json()[0]["id"], video["id"])

        public_preview = self.client.get(f"/api/v1/public/objects/{object_row['id']}/preview")
        self.assertEqual(public_preview.status_code, 200, public_preview.text)
        self.assertEqual(public_preview.json()["object"]["id"], object_row["id"])
        self.assertIsNone(public_preview.json()["object"].get("evidence_url"))
        self.assertEqual(public_preview.json()["video"]["id"], video["id"])
        self.assertEqual(public_preview.json()["transcript"]["id"], transcript["id"])
        self.assertEqual(len(public_preview.json()["annotations"]), 1)
        self.assertIsNone(public_preview.json()["annotations"][0].get("evidence_url"))
        preview_video_url = urlsplit(public_preview.json()["video_url"])
        self.assertEqual(preview_video_url.path, f"/api/v1/public/videos/{video['id']}/stream")
        self.assertRegex(preview_video_url.query, r"^v=(?:legacy|[0-9a-f]{12})$")
        self.assertTrue(public_preview.json()["model_url"].startswith(f"/api/v1/public/objects/{object_row['id']}/model/file"))

        public_search_surface = self.client.post(
            "/api/v1/public/search/segments",
            json={"query": "public memory", "project_id": project["id"], "page": 1, "page_size": 25},
        )
        self.assertEqual(public_search_surface.status_code, 200, public_search_surface.text)
        self.assertGreaterEqual(public_search_surface.json()["total_results"], 1)
        self.assertIsNone(public_search_surface.json()["search_log_id"])

        with SessionLocal() as db:
            query_log = (
                db.query(SearchQueryLog)
                .filter(SearchQueryLog.query_text == "public memory")
                .order_by(SearchQueryLog.created_at.desc())
                .first()
            )
            self.assertIsNotNone(query_log)
            self.assertEqual(query_log.query_source, "public_preview")

    def test_public_embed_manifest_fixture_validates(self):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "public_object_manifest_v1_demo.json"
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        manifest = PublicObjectManifestV1.model_validate(payload)

        self.assertEqual(manifest.id, "demo-vessel")
        self.assertEqual(manifest.manifestVersion, 1)
        self.assertEqual(len(manifest.clips), 1)
        self.assertEqual(manifest.annotations[0].relatedClipIds, ["clip-surface-interpretation"])

    def test_public_embed_manifest_smoke(self):
        candidate = self._publish_embed_candidate(website_object_id="demo-vessel")
        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])

        response = self.client.get("/api/public/objects/demo-vessel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertIsNone(response.headers.get("etag"))

        payload = response.json()
        manifest = PublicObjectManifestV1.model_validate(payload)
        self.assertEqual(manifest.id, "demo-vessel")
        self.assertEqual(manifest.provider, "Semantic")
        self.assertEqual(len(manifest.annotations), 2)
        self.assertEqual(len(manifest.clips), 1)
        self.assertEqual(manifest.clips[0].id, candidate["clips"][0]["website_clip_id"])
        self.assertEqual(manifest.annotations[0].relatedClipId, candidate["clips"][0]["website_clip_id"])
        self.assertEqual(manifest.annotations[0].relatedClipIds, [candidate["clips"][0]["website_clip_id"]])
        self.assertTrue(manifest.posterSrc.endswith("/api/public/objects/demo-vessel/poster"))
        self.assertTrue(manifest.modelSrc)
        self.assertTrue(
            manifest.modelSrc.startswith(
                f"http://localhost:8000/api/v1/public/objects/{candidate['object']['id']}/model/file?v="
            )
        )

        unknown_response = self.client.get("/api/public/objects/unknown-demo-object")
        self.assertEqual(unknown_response.status_code, 404)
        unknown_evidence = self.client.get("/api/v1/public/evidence/objects/unknown-demo-object")
        self.assertEqual(unknown_evidence.status_code, 404)

        poster_response = self.client.get("/api/public/objects/demo-vessel/poster")
        self.assertEqual(poster_response.status_code, 200, poster_response.text)
        self.assertEqual(poster_response.headers.get("cache-control"), "no-store")
        self.assertIn(
            "inline; filename=\"demo-vessel.svg\"",
            poster_response.headers.get("content-disposition", ""),
        )

        clip_response = self.client.get(f"/api/public/clips/{candidate['clips'][0]['website_clip_id']}/stream")
        self.assertEqual(clip_response.status_code, 200, clip_response.text)
        self.assertEqual(clip_response.headers.get("cache-control"), "no-store")

        clip_poster_response = self.client.get(f"/api/public/clips/{candidate['clips'][0]['website_clip_id']}/poster")
        self.assertEqual(clip_poster_response.status_code, 200, clip_poster_response.text)
        self.assertIn(
            f"inline; filename=\"{candidate['clips'][0]['website_clip_id']}.jpg\"",
            clip_poster_response.headers.get("content-disposition", ""),
        )

        unknown_response = self.client.get("/api/public/objects/obj-unknown")
        self.assertEqual(unknown_response.status_code, 404, unknown_response.text)

    def test_public_search_results_include_canonical_evidence_links(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-public-search-evidence-pilot")

        public_search_surface = self.client.post(
            "/api/v1/public/search/segments",
            json={
                "query": "object evidence",
                "project_id": candidate["project"]["id"],
                "page": 1,
                "page_size": 25,
            },
        )
        self.assertEqual(public_search_surface.status_code, 200, public_search_surface.text)
        self.assertGreaterEqual(public_search_surface.json()["total_results"], 1)

        result = public_search_surface.json()["results"][0]
        self.assertEqual(result["object_name"], candidate["object"]["name"])
        self.assertEqual(result["project_name"], candidate["project"]["name"])
        self.assertEqual(result["object_public_id"], candidate["website_object_id"])
        self.assertEqual(result["stable_video_id"], candidate["video"]["stable_video_id"])
        self.assertEqual(
            result["evidence_url"],
            f"/evidence/objects/{candidate['website_object_id']}?video={candidate['video']['stable_video_id']}&t=0",
        )

    def test_public_combined_search_backfills_visual_context_for_ranked_lexical_results(self):
        project = self._create_project("Public Combined Backfill Project")
        object_row = self._create_object(project["id"], "Public Combined Backfill Object")
        media_file = self._upload_media("public-combined-backfill.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "video-public-combined-backfill",
            "Public Combined Backfill Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            (
                "1\n00:00:00,000 --> 00:00:02,000\nspoon memory one\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\nspoon memory two\n\n"
                "3\n00:00:04,000 --> 00:00:06,000\nspoon memory three\n\n"
                "4\n00:00:06,000 --> 00:00:08,000\nspoon memory four\n\n"
                "5\n00:00:08,000 --> 00:00:10,000\nspoon memory five"
            ),
        )

        self._mark_video_ready(video["id"])

        for path in (
            f"/api/v1/objects/{object_row['id']}",
            f"/api/v1/videos/{video['id']}",
            f"/api/v1/transcripts/{transcript_payload['transcript']['id']}",
        ):
            response = self.client.patch(path, json={"is_published": True}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)

        query_embedding = [0.21] * settings.embedding_vector_dimensions
        window_thumbnail_paths = [
            Path(TEST_MEDIA_ROOT) / f"public-combined-backfill-thumb-{index}.jpg"
            for index in range(3)
        ]
        for path in window_thumbnail_paths:
            path.write_bytes(b"jpeg-bytes")

        with SessionLocal() as db:
            db_object = db.query(Object).filter_by(id=object_row["id"]).one()
            db_object.website_object_id = "obj-public-combined-backfill"
            db.add(db_object)

            segments = (
                db.query(Segment)
                .filter(Segment.transcript_id == transcript_payload["transcript"]["id"])
                .order_by(Segment.position.asc())
                .all()
            )
            self.assertEqual(len(segments), 5)

            windows = [
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=0,
                    start_position=segments[0].position,
                    end_position=segments[1].position,
                    start_ms=segments[0].start_ms,
                    end_ms=segments[1].end_ms,
                    text=f"{segments[0].text} {segments[1].text}",
                    embedding_vector=query_embedding,
                ),
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=1,
                    start_position=segments[1].position,
                    end_position=segments[3].position,
                    start_ms=segments[1].start_ms,
                    end_ms=segments[3].end_ms,
                    text=f"{segments[1].text} {segments[2].text} {segments[3].text}",
                    embedding_vector=query_embedding,
                ),
                TranscriptWindow(
                    transcript_id=transcript_payload["transcript"]["id"],
                    window_index=2,
                    start_position=segments[3].position,
                    end_position=segments[4].position,
                    start_ms=segments[3].start_ms,
                    end_ms=segments[4].end_ms,
                    text=f"{segments[3].text} {segments[4].text}",
                    embedding_vector=query_embedding,
                ),
            ]
            for window in windows:
                db.add(window)
            db.flush()

            for index, window in enumerate(windows):
                db.add(
                    VisualWindowDescription(
                        transcript_window_id=window.id,
                        transcript_id=transcript_payload["transcript"]["id"],
                        video_id=video["id"],
                        start_ms=window.start_ms,
                        end_ms=window.end_ms,
                        description_text=f"Window {index} visual context.",
                        embedding_vector=query_embedding,
                        thumbnail_path=str(window_thumbnail_paths[index]),
                        frame_manifest_json={"sample_frames": []},
                        generator_provider="openai",
                        generator_model="fake-vision-model",
                        embedding_provider="local",
                        embedding_model="fake-local-model",
                        status="ready",
                    )
                )
            db.commit()

        class FakeProvider:
            provider_name = "local"

            def embed_texts(self, texts):
                return SimpleNamespace(vectors=[query_embedding], model="fake-embedding-model", total_tokens=4)

        with patch("app.api.v1.endpoints.public.get_embedding_provider", return_value=FakeProvider()):
            public_search_surface = self.client.post(
                "/api/v1/public/search/segments",
                json={
                    "query": "spoon",
                    "project_id": project["id"],
                    "retrieval_mode": "combined",
                    "page": 1,
                    "page_size": 5,
                    "limit": 15,
                },
            )

        self.assertEqual(public_search_surface.status_code, 200, public_search_surface.text)
        payload = public_search_surface.json()
        self.assertEqual(payload["retrieval_mode"], "combined")
        self.assertEqual(len(payload["results"]), 5)
        self.assertTrue(all(result["context_start_ms"] is not None for result in payload["results"]))
        self.assertTrue(all(result["context_end_ms"] is not None for result in payload["results"]))
        self.assertTrue(all(result["thumbnail_url"] is not None for result in payload["results"]))
        self.assertTrue(all(result["scene_description"] is not None for result in payload["results"]))

    def test_public_object_list_exposes_canonical_evidence_links_for_manifest_backed_packages(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-public-browse-evidence-pilot")

        response = self.client.get("/api/v1/public/objects")
        self.assertEqual(response.status_code, 200, response.text)

        matching_object = next(
            obj for obj in response.json()
            if obj["id"] == candidate["object"]["id"]
        )
        self.assertEqual(
            matching_object["evidence_url"],
            f"/evidence/objects/{candidate['website_object_id']}",
        )
        self.assertEqual(
            matching_object["poster_url"],
            f"/api/public/objects/{candidate['website_object_id']}/poster",
        )

    def test_public_object_list_exposes_poster_urls_for_published_non_embed_ready_objects(self):
        candidate = self._publish_embed_candidate(
            website_object_id="obj-public-browse-pending-poster",
            publish_object=True,
            embed_ready=False,
        )

        response = self.client.get("/api/v1/public/objects")
        self.assertEqual(response.status_code, 200, response.text)

        matching_object = next(
            obj for obj in response.json()
            if obj["id"] == candidate["object"]["id"]
        )
        self.assertIsNone(matching_object["evidence_url"])
        self.assertEqual(
            matching_object["poster_url"],
            "/api/public/objects/obj-public-browse-pending-poster/poster",
        )

        poster_response = self.client.get("/api/public/objects/obj-public-browse-pending-poster/poster")
        self.assertEqual(poster_response.status_code, 200, poster_response.text)
        self.assertEqual(poster_response.headers.get("cache-control"), "no-store")

    def test_public_evidence_page_smoke(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-page-pilot")
        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-page-pilot")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        page = PublicEvidencePageResponse.model_validate(payload)
        self.assertEqual(page.object.id, "obj-evidence-page-pilot")
        self.assertEqual(page.object.poster_url, "/api/public/objects/obj-evidence-page-pilot/poster")
        object_poster = self.client.get(page.object.poster_url)
        self.assertEqual(object_poster.status_code, 200, object_poster.text)
        self.assertEqual(page.focus.source, "default")
        self.assertEqual(page.focus.annotation_id, annotation_ids[0])
        self.assertEqual(page.focus.clip_id, candidate["clips"][0]["website_clip_id"])
        self.assertEqual(page.focus.video_id, candidate["video"]["stable_video_id"])
        self.assertEqual(page.canonical_url, f"/evidence/objects/obj-evidence-page-pilot?annotation={annotation_ids[0]}")
        self.assertTrue(page.model.model_url.startswith(f"/api/v1/public/objects/{candidate['object']['id']}/model/file"))
        self.assertEqual(page.playback.video_id, candidate["video"]["stable_video_id"])
        self.assertEqual(page.selected_annotation.id, annotation_ids[0])
        self.assertEqual(page.selected_clip.id, candidate["clips"][0]["website_clip_id"])
        self.assertEqual(page.transcript.video_id, candidate["video"]["stable_video_id"])
        self.assertGreaterEqual(len(page.transcript.segments), 1)
        self.assertNotIn("id", payload["transcript"]["segments"][0])

    def test_public_evidence_page_omits_missing_object_poster(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-poster-missing")
        candidate["poster_path"].unlink()

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-poster-missing")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("poster_url", response.json()["object"])

    def test_public_evidence_page_never_exposes_poster_path_outside_media_root(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-poster-path")
        poster_path = candidate["poster_path"]
        with SessionLocal() as db:
            model = db.query(ObjectModel).filter(ObjectModel.object_id == candidate["object"]["id"]).one()
            model.public_poster_path = "/etc/passwd"
            db.commit()

        try:
            response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-poster-path")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("poster_url", response.json()["object"])

            manifest_response = self.client.get("/api/public/objects/obj-evidence-poster-path")
            self.assertEqual(manifest_response.status_code, 404, manifest_response.text)
            self.assertNotIn("/etc/passwd", manifest_response.text)

            poster_response = self.client.get("/api/public/objects/obj-evidence-poster-path/poster")
            self.assertEqual(poster_response.status_code, 404, poster_response.text)
            self.assertNotIn("private", poster_response.text)

            public_objects = self.client.get("/api/v1/public/objects")
            self.assertEqual(public_objects.status_code, 200, public_objects.text)
            listed_object = next(
                item for item in public_objects.json() if item["id"] == candidate["object"]["id"]
            )
            self.assertIsNone(listed_object.get("poster_url"))
        finally:
            with SessionLocal() as db:
                model = db.query(ObjectModel).filter(ObjectModel.object_id == candidate["object"]["id"]).one()
                model.public_poster_path = str(poster_path)
                db.commit()

    def test_public_preview_exposes_evidence_handoff_urls_for_manifest_backed_packages(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-handoff-pilot")
        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])

        response = self.client.get(f"/api/v1/public/objects/{candidate['object']['id']}/preview")
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["object"]["evidence_url"], "/evidence/objects/obj-evidence-handoff-pilot")
        self.assertEqual(
            {
                annotation["evidence_url"]
                for annotation in payload["annotations"]
                if annotation.get("evidence_url")
            },
            {
                f"/evidence/objects/obj-evidence-handoff-pilot?annotation={annotation_id}"
                for annotation_id in annotation_ids
            },
        )

    def test_public_evidence_page_focus_resolution(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-focus-pilot")
        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])
        clip_id = candidate["clips"][0]["website_clip_id"]
        video_id = candidate["video"]["stable_video_id"]

        clip_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-focus-pilot",
            params={"clip_id": clip_id},
        )
        self.assertEqual(clip_response.status_code, 200, clip_response.text)
        self.assertEqual(clip_response.json()["focus"]["source"], "clip")
        self.assertEqual(clip_response.json()["focus"]["clip_id"], clip_id)
        self.assertNotIn("selected_annotation", clip_response.json())

        annotation_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-focus-pilot",
            params={"annotation_id": annotation_ids[1]},
        )
        self.assertEqual(annotation_response.status_code, 200, annotation_response.text)
        self.assertEqual(annotation_response.json()["focus"]["source"], "annotation")
        self.assertEqual(annotation_response.json()["focus"]["annotation_id"], annotation_ids[1])

        video_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-focus-pilot",
            params={"video_id": video_id, "seek_ms": 1500},
        )
        self.assertEqual(video_response.status_code, 200, video_response.text)
        self.assertEqual(video_response.json()["focus"]["source"], "video")
        self.assertEqual(video_response.json()["focus"]["video_id"], video_id)
        self.assertEqual(video_response.json()["focus"]["seek_ms"], 1500)
        self.assertNotIn("selected_clip", video_response.json())

        timestamp_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-focus-pilot",
            params={"seek_ms": 750},
        )
        self.assertEqual(timestamp_response.status_code, 200, timestamp_response.text)
        self.assertEqual(timestamp_response.json()["focus"]["source"], "t")
        self.assertEqual(timestamp_response.json()["focus"]["video_id"], video_id)
        self.assertEqual(timestamp_response.json()["focus"]["seek_ms"], 750)
        self.assertEqual(
            timestamp_response.json()["canonical_url"],
            f"/evidence/objects/obj-evidence-focus-pilot?video={video_id}&t=750",
        )

    def test_public_evidence_page_uses_exact_title_card_observation_for_citation_attribution(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-exact")
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Synthetic Presenter Alpha",
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Synthetic Presenter Alpha - February 15, 2025",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-exact")
        self.assertEqual(response.status_code, 200, response.text)

        attribution = response.json()["citation_attribution"]
        self.assertNotIn("speaker_label", attribution)
        self.assertEqual(attribution["session_date"], "2025-02-15")
        self.assertEqual(attribution["session_date_text"], "February 15, 2025")
        self.assertEqual(attribution["session_date_precision"], "day")
        self.assertNotIn("source", attribution)
        self.assertNotIn("confidence", attribution)
        self.assertEqual(attribution["attribution_mode"], "unavailable")

    def test_public_evidence_page_omits_low_confidence_public_speaker_but_keeps_date_attribution(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-low-speaker")
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Synthetic Presenter Alpha",
            public_speaker_label="Synthetic Presenter Alpha",
            presenter_confidence="low",
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Synthetic Presenter Alpha - February 15, 2025",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-low-speaker")
        self.assertEqual(response.status_code, 200, response.text)

        attribution = response.json()["citation_attribution"]
        self.assertNotIn("speaker_label", attribution)
        self.assertNotIn("source", attribution)
        self.assertNotIn("confidence", attribution)
        self.assertEqual(attribution["session_date"], "2025-02-15")
        self.assertEqual(attribution["session_date_text"], "February 15, 2025")
        self.assertEqual(attribution["attribution_mode"], "unavailable")

    def test_public_evidence_page_uses_nearest_title_card_observation_when_exact_match_missing(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-nearest")
        video_id = candidate["video"]["stable_video_id"]

        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=500,
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="February 14, 2025",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=2400,
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="February 15, 2025",
        )

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-nearest",
            params={"video_id": video_id, "seek_ms": 2100},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["citation_attribution"]["session_date"], "2025-02-15")

    def test_public_evidence_page_selects_correct_title_card_segment_in_multi_speaker_video(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-multi")
        video_id = candidate["video"]["stable_video_id"]

        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Speaker One",
            public_speaker_label="Speaker One",
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="Speaker One - February 14, 2025",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=6000,
            presenter_name="Speaker Two",
            public_speaker_label="Speaker Two",
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Speaker Two - February 15, 2025",
        )

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-multi",
            params={"video_id": video_id, "seek_ms": 5800},
        )
        self.assertEqual(response.status_code, 200, response.text)

        attribution = response.json()["citation_attribution"]
        self.assertEqual(attribution["speaker_label"], "Speaker Two")
        self.assertNotIn("source", attribution)
        self.assertEqual(attribution["session_date"], "2025-02-15")
        self.assertNotIn("confidence", attribution)
        self.assertEqual(attribution["attribution_mode"], "named")

    def test_public_evidence_page_keeps_suppressed_intro_context_at_video_start(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-suppressed-intro")
        video_id = candidate["video"]["stable_video_id"]

        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Synthetic Producer",
            public_speaker_label=None,
            session_date_value=None,
            session_date_text=None,
            raw_text_observed="Produced by: Synthetic Producer",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=5000,
            presenter_name="Synthetic Presenter Gamma",
            public_speaker_label="Synthetic Presenter Gamma",
            session_date_value=date(2025, 2, 7),
            session_date_text="February 7, 2025",
            raw_text_observed="Synthetic Presenter Gamma - February 7, 2025",
        )

        start_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-suppressed-intro",
            params={"video_id": video_id, "seek_ms": 0},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)

        start_attribution = start_response.json()["citation_attribution"]
        self.assertNotIn("speaker_label", start_attribution)
        self.assertNotIn("source", start_attribution)
        self.assertNotIn("confidence", start_attribution)
        self.assertEqual(start_attribution["session_date"], "2025-02-07")
        self.assertEqual(start_attribution["session_date_text"], "February 7, 2025")
        self.assertEqual(start_attribution["attribution_mode"], "unavailable")
        self.assertEqual(start_attribution["session_date_precision"], "day")

        timeline = start_response.json()["citation_attribution_timeline"]
        self.assertGreaterEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["start_ms"], 0)
        self.assertEqual(timeline[0]["end_ms"], 5000)
        self.assertNotIn("speaker_label", timeline[0])
        self.assertEqual(timeline[0]["session_date"], "2025-02-07")
        self.assertEqual(timeline[0]["session_date_text"], "February 7, 2025")
        self.assertEqual(timeline[1]["start_ms"], 5000)
        self.assertEqual(timeline[1]["speaker_label"], "Synthetic Presenter Gamma")

        named_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-suppressed-intro",
            params={"video_id": video_id, "seek_ms": 5000},
        )
        self.assertEqual(named_response.status_code, 200, named_response.text)
        self.assertEqual(named_response.json()["citation_attribution"]["speaker_label"], "Synthetic Presenter Gamma")

    def test_public_evidence_page_uses_public_date_range_for_suppressed_intro_context_at_video_start(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-suppressed-intro-range")
        video_id = candidate["video"]["stable_video_id"]

        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Synthetic Producer",
            public_speaker_label=None,
            session_date_value=None,
            session_date_text=None,
            raw_text_observed="Produced by: Synthetic Producer",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=5000,
            presenter_name="Synthetic Presenter Gamma",
            public_speaker_label="Synthetic Presenter Gamma",
            session_date_value=date(2025, 2, 7),
            session_date_text="February 7, 2025",
            raw_text_observed="Synthetic Presenter Gamma - February 7, 2025",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=100000,
            presenter_name="Synthetic Presenter Delta",
            public_speaker_label="Synthetic Presenter Delta",
            session_date_value=date(2025, 2, 8),
            session_date_text="February 8, 2025",
            raw_text_observed="Synthetic Presenter Delta - February 8, 2025",
        )

        start_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-suppressed-intro-range",
            params={"video_id": video_id, "seek_ms": 0},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)

        start_attribution = start_response.json()["citation_attribution"]
        self.assertNotIn("speaker_label", start_attribution)
        self.assertNotIn("session_date", start_attribution)
        self.assertEqual(start_attribution["session_date_text"], "February 7, 2025 - February 8, 2025")
        self.assertEqual(start_attribution["session_date_precision"], "text")
        self.assertEqual(start_attribution["attribution_mode"], "unavailable")

        timeline = start_response.json()["citation_attribution_timeline"]
        self.assertGreaterEqual(len(timeline), 3)
        self.assertEqual(timeline[0]["start_ms"], 0)
        self.assertEqual(timeline[0]["end_ms"], 5000)
        self.assertNotIn("speaker_label", timeline[0])
        self.assertEqual(timeline[0]["session_date_text"], "February 7, 2025 - February 8, 2025")

        named_response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-attribution-suppressed-intro-range",
            params={"video_id": video_id, "seek_ms": 100000},
        )
        self.assertEqual(named_response.status_code, 200, named_response.text)
        self.assertEqual(named_response.json()["citation_attribution"]["speaker_label"], "Synthetic Presenter Delta")
        self.assertEqual(named_response.json()["citation_attribution"]["session_date_text"], "February 8, 2025")

    def test_public_evidence_page_exposes_public_safe_citation_attribution_timeline(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-timeline")
        video_id = candidate["video"]["stable_video_id"]

        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            presenter_name="Private OCR Person One",
            public_speaker_label="Speaker One",
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="Private OCR Person One - February 14, 2025",
            raw_payload_json={"presenter_name": "Private OCR Person One", "private_note": "do not expose"},
            source_kind="fixture_import",
            source_version="private-fixture-v1",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=6000,
            presenter_name="Private OCR Person Two",
            public_speaker_label="Speaker Two",
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Private OCR Person Two - February 15, 2025",
            raw_payload_json={"presenter_name": "Private OCR Person Two", "private_note": "do not expose"},
            source_kind="pilot_artifact",
            source_version="private-pilot-v1",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-timeline")
        self.assertEqual(response.status_code, 200, response.text)

        timeline = response.json()["citation_attribution_timeline"]
        self.assertGreaterEqual(len(timeline), 2)
        self.assertEqual({entry["speaker_label"] for entry in timeline}, {"Speaker One", "Speaker Two"})
        self.assertEqual({entry["video_id"] for entry in timeline}, {video_id})

        allowed_keys = {
            "video_id",
            "start_ms",
            "end_ms",
            "speaker_label",
            "session_date",
            "session_date_text",
        }
        for entry in timeline:
            self.assertLessEqual(set(entry), allowed_keys)
            self.assertNotIn("confidence", entry)
            self.assertNotIn("source", entry)
            self.assertNotIn("source_kind", entry)
            self.assertNotIn("source_version", entry)

        serialized_timeline = json.dumps(timeline, sort_keys=True)
        for forbidden_value in ("Private OCR Person One", "Private OCR Person Two", "do not expose"):
            self.assertNotIn(forbidden_value, serialized_timeline)
        for forbidden_field in (
            "presenter_name",
            "raw_text_observed",
            "raw_payload_json",
            "model_run_id",
            "source_version",
            "source_kind",
            "prompt_version",
            "model_provider",
            "model_name",
        ):
            self.assertNotIn(forbidden_field, serialized_timeline)
        self.assertIsNone(
            re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                serialized_timeline,
                flags=re.IGNORECASE,
            )
        )

    def test_public_evidence_page_ignores_pending_or_failed_title_card_observations(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-status-filter")
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="Pending card",
            status="pending",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=1000,
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Failed card",
            status="failed",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-status-filter")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("citation_attribution", response.json())

    def test_public_evidence_page_prefers_higher_priority_title_card_source_when_candidates_compete(self):
        self._clear_testclient_rate_limits()
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-source-priority")
        observation_kwargs = {
            "video_id": candidate["video"]["id"],
            "timestamp_ms": 0,
            "presenter_confidence": "high",
            "session_date_confidence": "high",
        }

        self._create_title_card_observation(
            **observation_kwargs,
            public_speaker_label="Fixture Speaker",
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="Fixture Speaker - February 14, 2025",
            source_kind="fixture_import",
            source_version="structured-json-v1",
        )
        self._create_title_card_observation(
            **observation_kwargs,
            public_speaker_label="Pilot Speaker",
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            raw_text_observed="Pilot Speaker - February 15, 2025",
            source_kind="pilot_artifact",
            source_version="2026-04-24-v1",
        )
        self._create_title_card_observation(
            **observation_kwargs,
            public_speaker_label="Vision Speaker",
            session_date_value=date(2025, 2, 16),
            session_date_text="February 16, 2025",
            raw_text_observed="Vision Speaker - February 16, 2025",
            source_kind="vision_ocr",
            source_version="cadence-5s-detail-high",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-source-priority")
        self.assertEqual(response.status_code, 200, response.text)

        attribution = response.json()["citation_attribution"]
        self.assertEqual(attribution["speaker_label"], "Vision Speaker")
        self.assertNotIn("source", attribution)
        self.assertNotIn("confidence", attribution)
        self.assertEqual(attribution["session_date"], "2025-02-16")
        self.assertEqual(attribution["attribution_mode"], "named")

    def test_public_evidence_page_omits_citation_attribution_when_no_title_card_observation_exists(self):
        self._publish_embed_candidate(website_object_id="obj-evidence-attribution-none")

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-none")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("citation_attribution", response.json())

    def test_public_evidence_page_suppresses_low_confidence_or_hidden_title_cards(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-attribution-suppressed")
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=0,
            title_card_visible=False,
            session_date_value=date(2025, 2, 14),
            session_date_text="February 14, 2025",
            raw_text_observed="Hidden card",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=1000,
            title_card_visible=True,
            session_date_value=date(2025, 2, 15),
            session_date_text="February 15, 2025",
            session_date_confidence="low",
            raw_text_observed="Low-confidence card",
        )

        response = self.client.get("/api/v1/public/evidence/objects/obj-evidence-attribution-suppressed")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("citation_attribution", response.json())

    def test_public_evidence_page_rejects_conflicting_focus_params(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-conflict-pilot")
        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])
        clip_id = candidate["clips"][0]["website_clip_id"]

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-conflict-pilot",
            params={"clip_id": clip_id, "annotation_id": annotation_ids[0]},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Conflicting focus parameters", response.json()["detail"])

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-conflict-pilot",
            params={"annotation_id": annotation_ids[0], "seek_ms": 500},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Conflicting focus parameters", response.json()["detail"])

    def test_public_evidence_page_rejects_cross_object_public_ids(self):
        first_candidate = self._publish_embed_candidate(website_object_id="obj-evidence-cross-a")
        second_candidate = self._publish_embed_candidate(website_object_id="obj-evidence-cross-b")

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-cross-a",
            params={"clip_id": second_candidate["clips"][0]["website_clip_id"]},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "Published clip not found for this object")

    def test_public_evidence_page_clip_focus_uses_unique_related_annotation_when_available(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-evidence-clip-annotation-pilot")
        second_clip = self._create_public_clip(
            candidate["video"]["id"],
            website_clip_id="clip-obj-evidence-clip-annotation-pilot-2",
            start_ms=5000,
            end_ms=9000,
        )

        self._set_embed_fields(
            project_id=candidate["project"]["id"],
            object_id=candidate["object"]["id"],
            annotation_ids=candidate["annotation_ids"],
            clip_ids=[candidate["clips"][0]["id"], second_clip["id"]],
            website_object_id="obj-evidence-clip-annotation-pilot",
            embed_ready=True,
            published=True,
            poster_path=candidate["poster_path"],
        )

        annotation_ids = self._public_annotation_ids(candidate["annotation_ids"])

        response = self.client.get(
            "/api/v1/public/evidence/objects/obj-evidence-clip-annotation-pilot",
            params={"clip_id": second_clip["website_clip_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["focus"]["source"], "clip")
        self.assertEqual(response.json()["focus"]["clip_id"], second_clip["website_clip_id"])
        self.assertEqual(response.json()["selected_annotation"]["id"], annotation_ids[1])

        self._publish_embed_candidate(
            website_object_id="obj-unpublished-pilot",
            publish_object=False,
            embed_ready=True,
        )
        unpublished_response = self.client.get("/api/public/objects/obj-unpublished-pilot")
        self.assertEqual(unpublished_response.status_code, 404, unpublished_response.text)

        self._publish_embed_candidate(
            website_object_id="obj-not-ready-pilot",
            publish_object=True,
            embed_ready=False,
        )
        not_ready_response = self.client.get("/api/public/objects/obj-not-ready-pilot")
        self.assertEqual(not_ready_response.status_code, 404, not_ready_response.text)

    def test_citation_attribution_suppresses_speaker_for_missing_leading_observations(self):
        # Synthetic gap scenario: the first eligible observation is NOT at t=0.
        # No leading-context card exists in the DB.  Seeks before the first
        # eligible observation must return no-speaker rather than inheriting
        # the adjacent named speaker via midpoint banding.
        candidate = self._publish_embed_candidate(website_object_id="obj-attrib-leading-gap")
        video_id = candidate["video"]["stable_video_id"]

        # Two rows at t=15000 and t=20000 (cadence=5000ms inferred).
        # Nothing at t=0, t=5000, t=10000 (suppressed/failed — not in DB).
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=15000,
            presenter_name="Bendu Sloteh",
            public_speaker_label="Bendu Sloteh",
            session_date_value=date(2025, 2, 21),
            session_date_text="February 21, 2025",
            raw_text_observed="Bendu Sloteh - February 21, 2025",
        )
        self._create_title_card_observation(
            video_id=candidate["video"]["id"],
            timestamp_ms=20000,
            presenter_name="Bendu Sloteh",
            public_speaker_label="Bendu Sloteh",
            session_date_value=date(2025, 2, 21),
            session_date_text="February 21, 2025",
            raw_text_observed="Bendu Sloteh - February 21, 2025",
        )

        for suppressed_ms in (0, 5000, 10000):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-leading-gap",
                params={"video_id": video_id, "seek_ms": suppressed_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={suppressed_ms}: {resp.text}")
            attrib = resp.json()["citation_attribution"]
            self.assertNotIn(
                "speaker_label", attrib,
                f"seek_ms={suppressed_ms} should produce no speaker_label, got {attrib}",
            )
            self.assertEqual(
                attrib["attribution_mode"], "unavailable",
                f"seek_ms={suppressed_ms} should be unavailable, got {attrib}",
            )

        named_resp = self.client.get(
            "/api/v1/public/evidence/objects/obj-attrib-leading-gap",
            params={"video_id": video_id, "seek_ms": 15000},
        )
        self.assertEqual(named_resp.status_code, 200, named_resp.text)
        self.assertEqual(named_resp.json()["citation_attribution"]["speaker_label"], "Bendu Sloteh")
        self.assertEqual(named_resp.json()["citation_attribution"]["attribution_mode"], "named")

    def test_citation_attribution_leading_gap_does_not_shadow_named_bands(self):
        # Regression for the index==0 wrap-around bug: when the first item is a
        # leading gap (is_gap=True at index 0), items[index-1] would wrap to items[-1]
        # and mistakenly classify the gap as inter-speaker, extending it to cover the
        # whole video.  With multiple speakers, prev_speaker (Sarah) != next_speaker
        # (Bendu) would fire and set end_ms to Sarah's last_ts + cadence (~200s).
        #
        # This test uses two speakers so the bug would fire and shadow all attribution.
        # After the fix, the leading gap ends at the correct half-cadence boundary and
        # named bands resolve normally.
        candidate = self._publish_embed_candidate(website_object_id="obj-attrib-leading-multi")
        video_id = candidate["video"]["stable_video_id"]

        # Speaker A at t=15000 and t=20000 — cadence inferred = 5000ms.
        # Nothing at t=0, t=5000, t=10000 (absent — leading gap inserted at index 0).
        for ts in (15000, 20000):
            self._create_title_card_observation(
                video_id=candidate["video"]["id"],
                timestamp_ms=ts,
                presenter_name="Speaker A",
                public_speaker_label="Speaker A",
                session_date_value=date(2025, 2, 21),
                session_date_text="February 21, 2025",
                raw_text_observed=f"Speaker A - February 21, 2025 t={ts}",
            )
        # Speaker B at t=35000 and t=40000 — inter-speaker gap at t=25000, t=30000.
        for ts in (35000, 40000):
            self._create_title_card_observation(
                video_id=candidate["video"]["id"],
                timestamp_ms=ts,
                presenter_name="Speaker B",
                public_speaker_label="Speaker B",
                session_date_value=date(2025, 2, 23),
                session_date_text="February 23, 2025",
                raw_text_observed=f"Speaker B - February 23, 2025 t={ts}",
            )

        # Leading gap (0–12500) must stay unavailable.
        for suppressed_ms in (0, 5000, 10000):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-leading-multi",
                params={"video_id": video_id, "seek_ms": suppressed_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={suppressed_ms}: {resp.text}")
            attrib = resp.json()["citation_attribution"]
            self.assertNotIn(
                "speaker_label", attrib,
                f"seek_ms={suppressed_ms} leading gap must not carry a speaker label, got {attrib}",
            )
            self.assertEqual(attrib["attribution_mode"], "unavailable")

        # Named bands must NOT be shadowed by the leading gap.
        for named_ms, expected_label in ((15000, "Speaker A"), (20000, "Speaker A"), (35000, "Speaker B"), (40000, "Speaker B")):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-leading-multi",
                params={"video_id": video_id, "seek_ms": named_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={named_ms}: {resp.text}")
            self.assertEqual(
                resp.json()["citation_attribution"]["speaker_label"], expected_label,
                f"seek_ms={named_ms} should be {expected_label} — leading gap must not shadow named bands",
            )

        # Inter-speaker gap half-cadence zone (22500–25000) remains unavailable.
        resp = self.client.get(
            "/api/v1/public/evidence/objects/obj-attrib-leading-multi",
            params={"video_id": video_id, "seek_ms": 23000},
        )
        self.assertEqual(resp.status_code, 200, f"seek_ms=23000: {resp.text}")
        gap_attrib = resp.json()["citation_attribution"]
        self.assertNotIn(
            "speaker_label", gap_attrib,
            f"seek_ms=23000 should be unavailable (inter-speaker half-cadence zone), got {gap_attrib}",
        )
        self.assertEqual(gap_attrib["attribution_mode"], "unavailable")

        # Speaker B band starts at prev_last + cadence_ms = 20000 + 5000 = 25000.
        resp = self.client.get(
            "/api/v1/public/evidence/objects/obj-attrib-leading-multi",
            params={"video_id": video_id, "seek_ms": 25000},
        )
        self.assertEqual(resp.status_code, 200, f"seek_ms=25000: {resp.text}")
        self.assertEqual(
            resp.json()["citation_attribution"]["speaker_label"], "Speaker B",
            "seek_ms=25000 should resolve to Speaker B — inter-speaker fix must still fire for mid-video gaps",
        )

    def test_citation_attribution_suppresses_speaker_for_missing_mid_video_observations(self):
        # Synthetic gap scenario: sampled timestamps are absent between two speaker groups
        # (t=10000 and t=15000 absent between Speaker A at t=5000 and Speaker B at t=20000).
        # With the inter-speaker gap fix, seeks from prev_last + cadence_ms (=10000) onward
        # resolve to the next speaker; only the half-cadence boundary zone (7500–10000) is
        # unavailable.
        candidate = self._publish_embed_candidate(website_object_id="obj-attrib-mid-gap")
        video_id = candidate["video"]["stable_video_id"]

        # Speaker A at t=0 and t=5000 (cadence=5000ms).  Gap at t=10000, t=15000.
        # Speaker B at t=20000 and t=25000.
        for ts in (0, 5000):
            self._create_title_card_observation(
                video_id=candidate["video"]["id"],
                timestamp_ms=ts,
                presenter_name="Speaker A",
                public_speaker_label="Speaker A",
                session_date_value=date(2025, 2, 21),
                session_date_text="February 21, 2025",
                raw_text_observed=f"Speaker A - February 21, 2025 t={ts}",
            )
        for ts in (20000, 25000):
            self._create_title_card_observation(
                video_id=candidate["video"]["id"],
                timestamp_ms=ts,
                presenter_name="Speaker B",
                public_speaker_label="Speaker B",
                session_date_value=date(2025, 2, 23),
                session_date_text="February 23, 2025",
                raw_text_observed=f"Speaker B - February 23, 2025 t={ts}",
            )

        for named_ms, expected_label in ((0, "Speaker A"), (5000, "Speaker A"), (20000, "Speaker B"), (25000, "Speaker B")):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-mid-gap",
                params={"video_id": video_id, "seek_ms": named_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={named_ms}: {resp.text}")
            self.assertEqual(
                resp.json()["citation_attribution"]["speaker_label"], expected_label,
                f"seek_ms={named_ms} should be {expected_label}",
            )

        # The half-cadence boundary zone (7500–10000) remains unavailable.
        resp = self.client.get(
            "/api/v1/public/evidence/objects/obj-attrib-mid-gap",
            params={"video_id": video_id, "seek_ms": 8000},
        )
        self.assertEqual(resp.status_code, 200, f"seek_ms=8000: {resp.text}")
        boundary_attrib = resp.json()["citation_attribution"]
        self.assertNotIn(
            "speaker_label", boundary_attrib,
            f"seek_ms=8000 should produce no speaker_label, got {boundary_attrib}",
        )
        self.assertEqual(boundary_attrib["attribution_mode"], "unavailable")

        # From prev_last + cadence_ms=10000 onward, seeks resolve to Speaker B.
        for spillover_ms in (10000, 15000):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-mid-gap",
                params={"video_id": video_id, "seek_ms": spillover_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={spillover_ms}: {resp.text}")
            self.assertEqual(
                resp.json()["citation_attribution"]["speaker_label"], "Speaker B",
                f"seek_ms={spillover_ms} should resolve to Speaker B after inter-speaker gap fix",
            )

    def test_citation_attribution_suppresses_speaker_for_missing_intra_group_observations(self):
        # Synthetic gap scenario: a sampled timestamp (t=15000) is absent from within a
        # single speaker's run (t=5000, t=10000, [gap], t=20000, t=25000).  The gap
        # must return no-speaker; the surrounding rows must still return the named
        # speaker.
        candidate = self._publish_embed_candidate(website_object_id="obj-attrib-intra-gap")
        video_id = candidate["video"]["stable_video_id"]

        for ts in (5000, 10000, 20000, 25000):
            self._create_title_card_observation(
                video_id=candidate["video"]["id"],
                timestamp_ms=ts,
                presenter_name="Nyomuree Saywrah",
                public_speaker_label="Nyomuree Saywrah",
                session_date_value=date(2025, 2, 23),
                session_date_text="February 23, 2025",
                raw_text_observed=f"Nyomuree Saywrah - February 23, 2025 t={ts}",
            )

        for named_ms in (5000, 10000, 20000, 25000):
            resp = self.client.get(
                "/api/v1/public/evidence/objects/obj-attrib-intra-gap",
                params={"video_id": video_id, "seek_ms": named_ms},
            )
            self.assertEqual(resp.status_code, 200, f"seek_ms={named_ms}: {resp.text}")
            self.assertEqual(
                resp.json()["citation_attribution"]["speaker_label"], "Nyomuree Saywrah",
                f"seek_ms={named_ms} should be Nyomuree Saywrah",
            )

        # t=15000 is absent from the DB (suppressed gap).
        suppressed_resp = self.client.get(
            "/api/v1/public/evidence/objects/obj-attrib-intra-gap",
            params={"video_id": video_id, "seek_ms": 15000},
        )
        self.assertEqual(suppressed_resp.status_code, 200, suppressed_resp.text)
        intra_attrib = suppressed_resp.json()["citation_attribution"]
        self.assertNotIn(
            "speaker_label", intra_attrib,
            f"seek_ms=15000 (missing intra-group) should produce no speaker_label, got {intra_attrib}",
        )
        self.assertEqual(intra_attrib["attribution_mode"], "unavailable")

    def test_z_public_projection_exports_embed_metadata_and_clips(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-projection-pilot")
        self._seed_visual_window_for_transcript(
            transcript_id=candidate["transcript"]["id"],
            video_id=candidate["video"]["id"],
            slug="obj-projection-pilot",
        )

        manifest, media_paths = build_projection()

        with SessionLocal() as db:
            visual_row = (
                db.query(VisualWindowDescription)
                .filter_by(transcript_id=candidate["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc(), VisualWindowDescription.id.asc())
                .first()
            )

        self.assertIsNotNone(visual_row)

        projected_project = next(item for item in manifest["projects"] if item["id"] == candidate["project"]["id"])
        self.assertEqual(projected_project["website_slug"], "project-obj-projection-pilot")

        projected_object = next(item for item in manifest["objects"] if item["id"] == candidate["object"]["id"])
        self.assertEqual(projected_object["website_object_id"], "obj-projection-pilot")
        self.assertTrue(projected_object["is_embed_ready"])

        projected_model = next(item for item in manifest["object_models"] if item["object_id"] == candidate["object"]["id"])
        self.assertTrue(projected_model["public_poster_relpath"].endswith("embed-posters/obj-projection-pilot.svg"))

        projected_clip = next(item for item in manifest["clips"] if item["id"] == candidate["clips"][0]["id"])
        self.assertEqual(projected_clip["website_clip_id"], candidate["clips"][0]["website_clip_id"])
        self.assertTrue(projected_clip["public_media_relpath"].endswith(f"clips/{candidate['clips'][0]['id']}.mp4"))
        self.assertTrue(projected_clip["public_poster_relpath"].endswith(f"clips/{candidate['clips'][0]['id']}.jpg"))
        self.assertEqual(projected_clip["transcript_excerpt"], "public clip transcript excerpt")

        projected_annotation = next(item for item in manifest["object_model_annotations"] if item["id"] == candidate["annotation_ids"][0])
        self.assertEqual(projected_annotation["clip_id"], candidate["clips"][0]["id"])
        self.assertIsNotNone(projected_annotation["website_annotation_id"])
        self.assertEqual(projected_annotation["playlist_json"][0]["clip_id"], candidate["clips"][0]["id"])

        projected_visual = next(
            item for item in manifest["visual_window_descriptions"]
            if item["transcript_window_id"] == str(visual_row.transcript_window_id)
        )
        self.assertEqual(projected_visual["description_text"], visual_row.description_text)
        self.assertIsNotNone(projected_visual["thumbnail_relpath"])
        self.assertGreaterEqual(len(projected_visual["frame_manifest_json"]["sample_frames"]), 1)

        self.assertIn(f"clips/{candidate['clips'][0]['id']}.mp4", media_paths)
        self.assertIn(f"clips/{candidate['clips'][0]['id']}.jpg", media_paths)
        self.assertIn(f"embed-posters/obj-projection-pilot.svg", media_paths)
        self.assertIn(projected_visual["thumbnail_relpath"], media_paths)
        self.assertIn(projected_visual["frame_manifest_json"]["sample_frames"][0]["path"], media_paths)

    def test_zz_public_projection_imports_embed_metadata_and_clips(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-import-pilot")
        self._seed_visual_window_for_transcript(
            transcript_id=candidate["transcript"]["id"],
            video_id=candidate["video"]["id"],
            slug="obj-import-pilot",
        )
        manifest, _ = build_projection()

        manifest_path = Path(TEST_MEDIA_ROOT) / "public-projection-import-smoke.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with SessionLocal() as db:
            project = db.get(Project, candidate["project"]["id"])
            object_row = db.get(Object, candidate["object"]["id"])
            model = db.query(ObjectModel).filter_by(object_id=candidate["object"]["id"]).one()
            annotation = db.get(ObjectModelAnnotation, candidate["annotation_ids"][0])
            clip = db.get(Clip, candidate["clips"][0]["id"])
            visual_rows = (
                db.query(VisualWindowDescription)
                .filter_by(transcript_id=candidate["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc(), VisualWindowDescription.id.asc())
                .all()
            )

            project.website_slug = None
            object_row.website_object_id = None
            object_row.is_embed_ready = False
            model.public_poster_path = None
            annotation.clip_id = None
            annotation.website_annotation_id = None
            annotation.website_relations_json = {"publication_ids": [], "project_ids": [], "location_ids": []}
            annotation.playlist_json = [
                {
                    "video_id": str(annotation.video_id),
                    "clip_id": None,
                    "transcript_segment_id": str(annotation.transcript_segment_id) if annotation.transcript_segment_id else None,
                    "label": annotation.title,
                    "start_ms": int(annotation.start_ms),
                    "end_ms": int(annotation.end_ms),
                }
            ]
            clip.website_clip_id = None
            clip.output_mp4_path = None
            clip.metadata_json_path = None
            clip.transcript_excerpt = None
            clip.citation_text = None
            for visual_row in visual_rows:
                visual_row.description_text = None
                visual_row.thumbnail_path = None
                visual_row.frame_manifest_json = None
                visual_row.status = "pending"
                db.add(visual_row)
            db.add(project)
            db.add(object_row)
            db.add(model)
            db.add(annotation)
            db.add(clip)
            db.commit()

        with patch.object(sys, "argv", ["import_public_projection", "--manifest", str(manifest_path)]):
            import_public_projection_module.main()

        with SessionLocal() as db:
            project = db.get(Project, candidate["project"]["id"])
            object_row = db.get(Object, candidate["object"]["id"])
            model = db.query(ObjectModel).filter_by(object_id=candidate["object"]["id"]).one()
            annotation = db.get(ObjectModelAnnotation, candidate["annotation_ids"][0])
            clip = db.get(Clip, candidate["clips"][0]["id"])
            visual_rows = (
                db.query(VisualWindowDescription)
                .filter_by(transcript_id=candidate["transcript"]["id"])
                .order_by(VisualWindowDescription.start_ms.asc(), VisualWindowDescription.id.asc())
                .all()
            )

            self.assertEqual(project.website_slug, "project-obj-import-pilot")
            self.assertEqual(object_row.website_object_id, "obj-import-pilot")
            self.assertTrue(object_row.is_embed_ready)
            self.assertTrue(model.public_poster_path.endswith("embed-posters/obj-import-pilot.svg"))
            self.assertEqual(annotation.clip_id, clip.id)
            self.assertIsNotNone(annotation.website_annotation_id)
            self.assertEqual(annotation.playlist_json[0]["clip_id"], candidate["clips"][0]["id"])
            self.assertEqual(clip.website_clip_id, candidate["clips"][0]["website_clip_id"])
            self.assertTrue(clip.output_mp4_path.endswith(f"clips/{candidate['clips'][0]['id']}.mp4"))
            self.assertTrue(clip.metadata_json_path.endswith(f"clips/{candidate['clips'][0]['id']}.transcript.txt"))
            self.assertEqual(clip.transcript_excerpt, "public clip transcript excerpt")
            self.assertTrue(visual_rows)
            self.assertTrue(all(row.description_text for row in visual_rows))
            self.assertTrue(all(row.thumbnail_path for row in visual_rows))
            self.assertTrue(all((row.frame_manifest_json or {}).get("sample_frames") for row in visual_rows))

        public_search_surface = self.client.post(
            "/api/v1/public/search/segments",
            json={
                "query": "object evidence",
                "project_id": candidate["project"]["id"],
                "retrieval_mode": "combined",
                "page": 1,
                "page_size": 5,
                "limit": 15,
            },
        )
        self.assertEqual(public_search_surface.status_code, 200, public_search_surface.text)
        self.assertTrue(any(result["thumbnail_url"] is not None for result in public_search_surface.json()["results"]))

    def test_zz_public_sync_persists_publication_manifest_rows(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-manifest-writer-pilot")

        def _fake_ssh(config, remote_command, *, timeout=900):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        actor = {
            "type": "user",
            "user_id": "operator-123",
            "email": self.user_email,
            "trigger": "test.public_sync.persist",
        }
        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value={},
        ), patch.object(public_sync_module, "_ssh", side_effect=_fake_ssh), patch.object(
            public_sync_module,
            "_scp",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            result = public_sync_module.sync_public_projection(actor=actor)

        self.assertTrue(result.manifest_changed)
        with SessionLocal() as db:
            rows = (
                db.query(PublicationManifest)
                .filter(PublicationManifest.root_public_id == candidate["website_object_id"])
                .order_by(PublicationManifest.generated_at.asc(), PublicationManifest.id.asc())
                .all()
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].actor_json["email"], self.user_email)
            self.assertEqual(rows[0].actor_json["trigger"], "test.public_sync.persist")
            self.assertEqual(rows[0].package_type, "object_package")
            self.assertTrue(rows[0].is_current)
            self.assertIsNotNone(rows[0].projected_at)
            self.assertEqual(rows[0].canonical_path, "/evidence/objects/obj-manifest-writer-pilot")

            manifest_events = (
                db.query(AuditEvent)
                .filter(AuditEvent.root_public_id == candidate["website_object_id"])
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            manifest_event_types = {event.event_type for event in manifest_events}
            self.assertIn("publication_manifest.created", manifest_event_types)
            self.assertIn("publication_manifest.projected", manifest_event_types)

            sync_events = (
                db.query(AuditEvent)
                .filter(AuditEvent.subject_type == "public_sync")
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            self.assertTrue(any(event.event_type == "public_sync.completed" for event in sync_events))

    def test_zz_public_sync_reuses_current_manifest_for_unchanged_package(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-manifest-reuse-pilot")
        remote_state = {}

        def _fake_ssh(config, remote_command, *, timeout=900):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        actor = {"type": "user", "user_id": "operator-123", "email": self.user_email, "trigger": "test.public_sync.reuse"}
        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value={},
        ), patch.object(public_sync_module, "_ssh", side_effect=_fake_ssh), patch.object(
            public_sync_module,
            "_scp",
            side_effect=self._capture_public_sync_state(remote_state),
        ):
            first_result = public_sync_module.sync_public_projection(actor=actor)

        self.assertIn("projection_digest", remote_state)
        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value=remote_state,
        ), patch.object(public_sync_module, "_ssh", side_effect=_fake_ssh), patch.object(
            public_sync_module,
            "_scp",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            second_result = public_sync_module.sync_public_projection(actor=actor)

        self.assertTrue(first_result.manifest_changed)
        self.assertFalse(second_result.manifest_changed)
        with SessionLocal() as db:
            rows = (
                db.query(PublicationManifest)
                .filter(PublicationManifest.root_public_id == candidate["website_object_id"])
                .order_by(PublicationManifest.generated_at.asc(), PublicationManifest.id.asc())
                .all()
            )
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].is_current)

            sync_events = (
                db.query(AuditEvent)
                .filter(AuditEvent.subject_type == "public_sync")
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            self.assertTrue(any(event.event_type == "public_sync.noop" for event in sync_events))

    def test_zz_public_sync_supersedes_changed_manifest_rows(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-manifest-supersede-pilot")
        first_remote_state = {}

        def _fake_ssh(config, remote_command, *, timeout=900):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        actor = {"type": "user", "user_id": "operator-123", "email": self.user_email, "trigger": "test.public_sync.supersede"}
        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value={},
        ), patch.object(public_sync_module, "_ssh", side_effect=_fake_ssh), patch.object(
            public_sync_module,
            "_scp",
            side_effect=self._capture_public_sync_state(first_remote_state),
        ):
            public_sync_module.sync_public_projection(actor=actor)

        self.assertIn("projection_digest", first_remote_state)

        object_patch = self.client.patch(
            f"/api/v1/objects/{candidate['object']['id']}",
            json={"description": "Updated description for supersession coverage."},
            headers=self.headers,
        )
        self.assertEqual(object_patch.status_code, 200, object_patch.text)

        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value=first_remote_state,
        ), patch.object(public_sync_module, "_ssh", side_effect=_fake_ssh), patch.object(
            public_sync_module,
            "_scp",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            public_sync_module.sync_public_projection(actor=actor)

        with SessionLocal() as db:
            rows = (
                db.query(PublicationManifest)
                .filter(PublicationManifest.root_public_id == candidate["website_object_id"])
                .order_by(PublicationManifest.generated_at.asc(), PublicationManifest.id.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(1 for row in rows if row.is_current), 1)
            previous_row = next(row for row in rows if not row.is_current)
            current_row = next(row for row in rows if row.is_current)
            self.assertIsNotNone(previous_row.superseded_at)
            self.assertIsNotNone(previous_row.projected_at)
            self.assertIsNotNone(current_row.projected_at)
            self.assertNotEqual(previous_row.projection_digest, current_row.projection_digest)

            manifest_events = (
                db.query(AuditEvent)
                .filter(AuditEvent.root_public_id == candidate["website_object_id"])
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            self.assertTrue(any(event.event_type == "publication_manifest.superseded" for event in manifest_events))

    def test_zz_public_sync_leaves_projected_at_null_until_remote_import_succeeds(self):
        candidate = self._publish_embed_candidate(website_object_id="obj-manifest-failure-pilot")

        def _failing_ssh(config, remote_command, *, timeout=900):
            if "import_public_projection" in remote_command:
                raise RuntimeError("synthetic remote import failure")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        actor = {"type": "user", "user_id": "operator-123", "email": self.user_email, "trigger": "test.public_sync.failure"}
        with patch.object(public_sync_module, "_load_sync_config", return_value=self._public_sync_config()), patch.object(
            public_sync_module,
            "_load_remote_sync_state",
            return_value={},
        ), patch.object(public_sync_module, "_ssh", side_effect=_failing_ssh), patch.object(
            public_sync_module,
            "_scp",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            with self.assertRaises(RuntimeError):
                public_sync_module.sync_public_projection(actor=actor)

        with SessionLocal() as db:
            rows = (
                db.query(PublicationManifest)
                .filter(PublicationManifest.root_public_id == candidate["website_object_id"])
                .order_by(PublicationManifest.generated_at.asc(), PublicationManifest.id.asc())
                .all()
            )
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].is_current)
            self.assertIsNone(rows[0].projected_at)

            sync_events = (
                db.query(AuditEvent)
                .filter(AuditEvent.subject_type == "public_sync")
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all()
            )
            self.assertTrue(any(event.event_type == "public_sync.failed" for event in sync_events))

    def test_search_tuning_summary_surfaces_live_feedback(self):
        project = self._create_project("Tuning Summary Project")
        object_row = self._create_object(project["id"], "Tuning Summary Object")
        media_file = self._upload_media("tuning-summary-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-tuning-summary-video",
            "Tuning Summary Video",
        )["video"]

        self._ingest_transcript(
            video["id"],
            "1\n00:00:00,000 --> 00:00:02,000\nfatherhood memory\n\n2\n00:00:02,000 --> 00:00:04,000\nritual practice",
        )

        with patch("app.api.v1.endpoints.search.get_embedding_provider", side_effect=AIProviderError("semantic disabled")):
            search_response = self.client.post(
                "/api/v1/search/segments",
                json={"query": "fatherhood", "project_id": project["id"], "page": 1, "page_size": 25},
                headers=self.headers,
            )

        self.assertEqual(search_response.status_code, 200, search_response.text)
        first_result = search_response.json()["results"][0]
        feedback_response = self.client.post(
            "/api/v1/search/feedback",
            json={
                "search_query_log_id": search_response.json()["search_log_id"],
                "segment_id": first_result["segment_id"],
                "is_relevant": True,
                "lexical_match": first_result["lexical_match"],
                "semantic_score": first_result["semantic_score"],
                "rank_score": first_result["rank_score"],
                "rank_position": 1,
            },
            headers=self.headers,
        )
        self.assertEqual(feedback_response.status_code, 200, feedback_response.text)

        summary_response = self.client.get("/api/v1/search/tuning/summary", headers=self.headers)
        self.assertEqual(summary_response.status_code, 200, summary_response.text)
        payload = summary_response.json()
        self.assertGreaterEqual(payload["live_window"]["new_vote_count"], 1)
        self.assertTrue(any(item["canonical_query"] == "fatherhood" for item in payload["live_window"]["canonical_queries"]))
        self.assertTrue(any(item["query_text"] == "fatherhood" for item in payload["live_window"]["recent_feedback"]))

    def test_search_tuning_report_requires_threshold_and_can_be_approved(self):
        project = self._create_project("Tuning Report Project")
        object_row = self._create_object(project["id"], "Tuning Report Object")
        media_file = self._upload_media("tuning-report-sample.mp4")
        video = self._create_video(
            project["id"],
            object_row["id"],
            media_file["path"],
            "smoke-tuning-report-video",
            "Tuning Report Video",
        )["video"]

        transcript_payload = self._ingest_transcript(
            video["id"],
            (
                "1\n00:00:00,000 --> 00:00:02,000\nquery seed one\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\nquery seed two\n\n"
                "3\n00:00:04,000 --> 00:00:06,000\nquery seed three"
            ),
        )

        with SessionLocal() as db:
            user = db.query(User).filter(User.email == self.user_email).first()
            self.assertIsNotNone(user)
            segments = (
                db.query(Segment)
                .filter(Segment.transcript_id == transcript_payload["transcript"]["id"])
                .order_by(Segment.position.asc())
                .all()
            )
            self.assertEqual(len(segments), 3)

            for query_index in range(10):
                canonical_query = f"tuning cluster {query_index}"
                for run_index in range(2):
                    query_log = SearchQueryLog(
                        user_id=user.id,
                        project_id=None,
                        video_id=None,
                        query_text=canonical_query,
                        query_source="analysis",
                        result_count=3,
                        lexical_result_count=0,
                        semantic_result_count=3,
                        semantic_attempted=True,
                        semantic_succeeded=True,
                        semantic_provider="local",
                        semantic_model="smoke-model",
                        retrieval_mode="window_first",
                    )
                    db.add(query_log)
                    db.flush()

                    for rank_position, segment in enumerate(segments, start=1):
                        db.add(
                            SearchResultFeedback(
                                user_id=user.id,
                                search_query_log_id=query_log.id,
                                segment_id=segment.id,
                                is_relevant=rank_position != 3,
                                lexical_match=False,
                                semantic_score=0.76 if rank_position == 1 else 0.7,
                                rank_score=1.0 - (rank_position * 0.1),
                                rank_position=rank_position,
                            )
                        )
            db.commit()

        summary_response = self.client.get("/api/v1/search/tuning/summary", headers=self.headers)
        self.assertEqual(summary_response.status_code, 200, summary_response.text)
        self.assertTrue(summary_response.json()["live_window"]["ready_for_report"])

        create_report_response = self.client.post("/api/v1/search/tuning/reports", headers=self.headers)
        self.assertEqual(create_report_response.status_code, 200, create_report_response.text)
        report = create_report_response.json()
        self.assertGreaterEqual(report["new_vote_count"], 50)
        self.assertGreaterEqual(report["recurring_canonical_query_count"], 10)
        self.assertEqual(report["status"], "DRAFT")

        approve_response = self.client.post(
            f"/api/v1/search/tuning/reports/{report['id']}/approve-live-comparison",
            headers=self.headers,
        )
        self.assertEqual(approve_response.status_code, 200, approve_response.text)
        self.assertEqual(approve_response.json()["status"], "APPROVED_FOR_LIVE_COMPARISON")
        self.assertIsNotNone(approve_response.json()["approved_at"])


if __name__ == "__main__":
    unittest.main()
