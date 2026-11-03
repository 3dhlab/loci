"""Real disposable PostgreSQL re-ingest/ordinal/rollback regressions.

Authored for CI, not a claim of local execution. These invoke the actual
import_projection in a caller-owned transaction with real immediate uniques
and FKs. No SSH, media encoder, application server or production service runs.
"""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4


def _require_disposable_database(raw):
    try:
        parsed = urlsplit(raw)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        service = parsed.hostname == "postgres" and os.environ.get("CI_DISPOSABLE_SERVICES") == "true"
        if (os.environ.get("SEMANTIC_ENV", "development") != "development"
                or parsed.scheme not in {"postgresql", "postgresql+psycopg"}
                or not (local or service) or not parsed.username
                or parsed.path != "/semantic_ci" or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
        return raw
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("Ordinal regression tests require explicit CI_DATABASE_URL for the disposable local semantic_ci database in development.") from None


class PublicProjectionOrdinalPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Target validation precedes database/client/application imports.
        cls.url = _require_disposable_database(os.environ.get("CI_DATABASE_URL", ""))
        from sqlalchemy import create_engine, text
        cls.admin = create_engine(cls.url)
        with cls.admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.db import base  # Register FK models without starting a service.
        from app.models import entities as models
        from app.models.publication_job import PublicationGeneration
        from app.scripts import import_public_projection as importer
        self.models = models; self.importer = importer
        self.schema = "loci_ordinal_test_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(self.url, connect_args={"options": f"-csearch_path={self.schema},public"})
        self.addCleanup(self._cleanup_schema)
        # Same ORM constraints as the application; migration-graph acceptance is
        # separate. Only importer tables and their FK dependencies are created.
        names = ("User", "Project", "Object", "Video", "Transcript", "Segment", "TranscriptWindow",
                 "ModelProvider", "ModelRun", "VisualWindowDescription", "CorpusPhrase", "ObjectModel",
                 "Clip", "ObjectModelAnnotation")
        tables = [getattr(models, name).__table__ for name in names] + [PublicationGeneration.__table__]
        base.Base.metadata.create_all(self.engine, tables=tables)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.temp = tempfile.TemporaryDirectory(prefix="loci-ordinal-pg-", dir=os.environ.get("LOCI_TEST_FILE_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(importer.settings, "media_root", self.temp.name).start()
        patch.object(importer.settings, "transcode_output_subdir", "transcoded").start()
        self.ids = {name: str(uuid4()) for name in ("project", "object", "video", "transcript", "model", "clip", "annotation")}
        with self.Session.begin() as db:
            db.add(models.User(id=importer.PUBLIC_OWNER_ID, email=importer.PUBLIC_OWNER_EMAIL,
                               password_hash="synthetic-disabled-fixture-only", is_active=False))

    def _cleanup_schema(self):
        from sqlalchemy import text
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def payload(self, *, segments=None, windows=None, visuals=None):
        ids = self.ids
        segments = segments or [str(uuid4()), str(uuid4())]
        windows = windows or [str(uuid4()), str(uuid4())]
        visuals = visuals or [str(uuid4()), str(uuid4())]
        return {"projects": [{"id": ids["project"], "name": "Synthetic ordinal project"}],
            "objects": [{"id": ids["object"], "project_id": ids["project"], "name": "Synthetic ordinal object",
                         "website_object_id": "synthetic-ordinal-object", "is_published": True}],
            "videos": [{"id": ids["video"], "project_id": ids["project"], "object_id": ids["object"],
                "stable_video_id": "synthetic-ordinal-video", "title": "Synthetic ordinal video",
                "original_filename": "synthetic.mp4", "public_media_relpath": f"transcoded/{ids['video']}.mp4",
                "sha256_checksum": "a" * 64, "duration_ms": 2000, "status": "READY", "is_published": True,
                "publication_attribution_json": []}],
            "transcripts": [{"id": ids["transcript"], "video_id": ids["video"], "title": "Synthetic transcript",
                "source": "MANUAL", "language": "en", "format": "PLAIN", "raw_text": "Synthetic two-section text.",
                "is_published": True}],
            "segments": [{"id": ident, "transcript_id": ids["transcript"], "position": index,
                "start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"Synthetic segment {index}."}
                for index, ident in enumerate(segments)],
            "transcript_windows": [{"id": ident, "transcript_id": ids["transcript"], "window_index": index,
                "start_position": index, "end_position": index, "start_ms": index * 1000,
                "end_ms": (index + 1) * 1000, "text": f"Synthetic window {index}."}
                for index, ident in enumerate(windows)],
            "visual_window_descriptions": [{"id": ident, "transcript_window_id": windows[index],
                "transcript_id": ids["transcript"], "video_id": ids["video"], "start_ms": index * 1000,
                "end_ms": (index + 1) * 1000, "description_text": f"Synthetic visual {index}.", "status": "ready"}
                for index, ident in enumerate(visuals)],
            "object_models": [{"id": ids["model"], "object_id": ids["object"],
                "public_media_relpath": "synthetic/cube.glb", "original_filename": "cube.glb", "mime_type": "model/gltf-binary",
                "sha256_checksum": "b" * 64, "file_size_bytes": 16, "revision_number": 1,
                "position_x": 0, "position_y": 0, "position_z": 0, "rotation_x": 0, "rotation_y": 0, "rotation_z": 0}],
            "clips": [{"id": ids["clip"], "video_id": ids["video"], "project_id": ids["project"],
                "start_ms": 0, "end_ms": 1000, "public_media_relpath": "synthetic/clip.mp4",
                "website_clip_id": "synthetic-ordinal-clip", "status": "COMPLETE"}],
            "object_model_annotations": [{"id": ids["annotation"], "object_model_id": ids["model"],
                "object_id": ids["object"], "video_id": ids["video"], "clip_id": ids["clip"],
                "transcript_segment_id": segments[0], "title": "Synthetic retained annotation",
                "point_x": 0, "point_y": 0, "point_z": .5, "start_ms": 0, "end_ms": 1000,
                "review_status": "ACTIVE", "is_published": True, "model_revision_created_against": 1,
                "website_annotation_id": "synthetic-ordinal-annotation"}]}

    def import_and_commit(self, payload):
        with self.Session.begin() as db:
            self.importer.import_projection(payload, db)

    def state(self):
        from sqlalchemy import select
        m = self.models
        with self.Session() as db:
            return {"segments": sorted((str(r.id), r.position, r.text) for r in db.scalars(select(m.Segment))),
                "windows": sorted((str(r.id), r.window_index, r.text) for r in db.scalars(select(m.TranscriptWindow))),
                "visuals": sorted((str(r.id), str(r.transcript_window_id)) for r in db.scalars(select(m.VisualWindowDescription))),
                "annotations": sorted((str(r.id), str(r.transcript_segment_id), str(r.clip_id)) for r in db.scalars(select(m.ObjectModelAnnotation))),
                "clips": sorted(str(r.id) for r in db.scalars(select(m.Clip))),
                "transcripts": sorted(str(r.id) for r in db.scalars(select(m.Transcript)))}

    def test_two_generations_replace_child_uuids_at_same_keys_and_rebind_retained_annotation(self):
        first = self.payload(); self.import_and_commit(first)
        second = self.payload(); self.import_and_commit(second)
        current = self.state()
        self.assertEqual({row[0] for row in current["segments"]}, {row["id"] for row in second["segments"]})
        self.assertEqual({row[0] for row in current["windows"]}, {row["id"] for row in second["transcript_windows"]})
        self.assertEqual(current["annotations"], [(self.ids["annotation"], second["segments"][0]["id"], self.ids["clip"])])
        self.assertEqual(current["clips"], [self.ids["clip"]])
        self.assertEqual(current["transcripts"], [self.ids["transcript"]])
        self.assertEqual({row[1] for row in current["visuals"]}, {row["id"] for row in second["transcript_windows"]})

    def test_retained_ids_swap_ordinals_with_loaded_orm_rows_and_preserve_fk_dependants(self):
        from sqlalchemy import select
        first = self.payload(); self.import_and_commit(first)
        second = self.payload(segments=[row["id"] for row in reversed(first["segments"])],
            windows=[row["id"] for row in reversed(first["transcript_windows"])],
            visuals=[row["id"] for row in reversed(first["visual_window_descriptions"])])
        with self.Session.begin() as db:
            # Exercise synchronize_session against identities a caller loaded.
            segments = list(db.scalars(select(self.models.Segment)))
            windows = list(db.scalars(select(self.models.TranscriptWindow)))
            self.importer.import_projection(second, db)
            self.assertEqual({str(row.id): row.position for row in segments},
                             {row["id"]: row["position"] for row in second["segments"]})
            self.assertEqual({str(row.id): row.window_index for row in windows},
                             {row["id"]: row["window_index"] for row in second["transcript_windows"]})
        self.assertEqual({row[0] for row in self.state()["segments"]}, {row["id"] for row in first["segments"]})

    def test_mixed_retained_and_new_ids_can_reuse_each_others_final_slots(self):
        first = self.payload(); self.import_and_commit(first)
        second = self.payload(segments=[first["segments"][1]["id"], str(uuid4())],
            windows=[first["transcript_windows"][1]["id"], str(uuid4())],
            visuals=[first["visual_window_descriptions"][1]["id"], str(uuid4())])
        self.import_and_commit(second)
        self.assertEqual({row[0] for row in self.state()["segments"]}, {row["id"] for row in second["segments"]})

    def test_late_constraint_failure_rolls_back_pruning_parking_and_new_rows(self):
        from sqlalchemy.exc import IntegrityError
        first = self.payload(); self.import_and_commit(first); original = self.state()
        invalid = self.payload()
        invalid["object_model_annotations"][0]["clip_id"] = str(uuid4())
        with self.assertRaises(IntegrityError):
            self.import_and_commit(invalid)
        self.assertEqual(self.state(), original)

    def test_retained_child_reparent_is_rejected_before_any_import_mutation(self):
        first = self.payload(); self.import_and_commit(first); original = self.state()
        for collection, parent in (("segments", "transcript_id"), ("transcript_windows", "transcript_id"),
                                   ("visual_window_descriptions", "transcript_window_id")):
            with self.subTest(collection=collection):
                invalid = deepcopy(first); invalid[collection][0][parent] = str(uuid4())
                with self.Session() as db:
                    with self.assertRaisesRegex(ValueError, "retained transcript child"):
                        self.importer.import_projection(invalid, db)
                    # No writes were pending; committing this read-only failure
                    # still leaves the exact original graph untouched.
                    db.commit()
                self.assertEqual(self.state(), original)


if __name__ == "__main__":
    unittest.main()
