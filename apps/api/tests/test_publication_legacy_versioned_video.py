"""Legacy import + actual playback resolver, using generated local files only.

The importer function body is unchanged by this harness. Database/query/model
boundaries are injected; these are not real SQLAlchemy, DB or HTTP tests.
"""
import ast
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from app.services.publication_media_inventory import MediaInventoryError, build_media_inventory
from app.services.publication_attribution import validate_public_attribution_bands


def importer_functions(media_root, subdir="transcoded"):
    path = Path(__file__).parents[1] / "app/scripts/import_public_projection.py"
    tree = ast.parse(path.read_text())
    names = {"_parse_dt", "_public_media_path", "_public_media_path_or_none",
             "_rehydrate_visual_frame_manifest", "_legacy_playback_identity", "import_projection"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    env = {"datetime": datetime, "Path": Path, "uuid": uuid, "json": json, "re": re,
           "settings": SimpleNamespace(media_root=media_root),
           "transcode_output_dir": lambda: media_root / subdir,
           "MediaInventoryError": MediaInventoryError, "build_media_inventory": build_media_inventory,
           "validate_public_attribution_bands": validate_public_attribution_bands}
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), str(path), "exec"), env)
    return env


def playback_resolver(media_root, subdir="transcoded"):
    path = Path(__file__).parents[1] / "app/services/media_transcode.py"
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "video_playback_path")
    env = {"Path": Path, "re": re, "settings": SimpleNamespace(media_root=media_root),
           "transcode_output_dir": lambda: media_root / subdir,
           "transcode_output_path": lambda ident: media_root / subdir / f"{ident}.mp4"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), env)
    return env["video_playback_path"]


class LegacyVersionedVideoImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-legacy-versioned-",
            dir=os.environ.get("LOCI_TEST_FILE_ROOT") or ("/private/tmp" if os.path.isdir("/private/tmp") else None))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "transcoded").mkdir()
        self.video_id = uuid.uuid4()
        self.data = b"generated synthetic normalized playback bytes"
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.name = f"{self.video_id}.{self.sha}.{'e' * 32}.mp4"
        self.relative = "transcoded/" + self.name
        self.path = self.root / self.relative
        self.path.write_bytes(self.data)
        self.env = importer_functions(self.root)
        self.resolve = playback_resolver(self.root)
        self.existing = None
        self.added = []
        test = self

        class Video:
            def __init__(self, **values):
                self.created_at = self.updated_at = None
                self.__dict__.update(values)

        class Status(str, Enum):
            READY = "READY"

        class DB:
            def get(self, model, ident):
                return test.existing if model is Video else None
            def add(self, row): test.added.append(row)
            def flush(self): pass
            def execute(self, statement): return SimpleNamespace(all=lambda: [])

        self.video_class = Video
        self.db = DB()
        self.env.update(Video=Video, VideoStatus=Status, PUBLIC_OWNER_ID=uuid.UUID(int=1),
            _ensure_public_owner=lambda db: None, _delete_missing=lambda *args: None)
        for name in ("Project", "Object", "Transcript", "Segment", "TranscriptWindow",
                     "VisualWindowDescription", "CorpusPhrase", "ObjectModel", "Clip", "ObjectModelAnnotation"):
            self.env[name] = type(name, (), {"id": None})
        self.sql = ModuleType("sqlalchemy")
        self.sql.select = lambda *args: object()

    def manifest(self, relative=None):
        result = {name: [] for name in ("projects", "objects", "videos", "transcripts", "segments",
            "transcript_windows", "object_models", "object_model_annotations")}
        result["videos"] = [{"id": str(self.video_id), "project_id": str(uuid.uuid4()),
            "object_id": str(uuid.uuid4()), "stable_video_id": "generated-video", "title": "Generated video",
            "original_filename": "generated-preservation.mov", "public_media_relpath": relative or self.relative,
            "sha256_checksum": "f" * 64, "duration_ms": 1000, "status": "READY", "is_published": True,
            "publication_attribution_json": []}]
        return result

    def run_import(self, relative=None, **kwargs):
        return self.run_payload(self.manifest(relative), **kwargs)

    def run_payload(self, payload, **kwargs):
        with patch.dict(sys.modules, {"sqlalchemy": self.sql}):
            self.env["import_projection"](payload, self.db, **kwargs)
        return next(row for row in self.added if isinstance(row, self.video_class))

    def test_versioned_manifest_import_resolves_exact_selected_file_not_legacy_name(self):
        video = self.run_import()
        self.assertEqual(video.playback_storage_key, self.name)
        self.assertEqual(video.playback_sha256_checksum, self.sha)
        self.assertEqual(video.playback_file_size_bytes, len(self.data))
        self.assertEqual(self.resolve(video), self.path)
        self.assertNotEqual(self.resolve(video), self.root / "transcoded" / f"{self.video_id}.mp4")

    def test_preservation_checksum_is_preserved_but_never_used_as_playback_checksum(self):
        video = self.run_import()
        self.assertEqual(video.sha256_checksum, "f" * 64)
        self.assertNotEqual(video.sha256_checksum, video.playback_sha256_checksum)
        self.assertIsNone(video.playback_recipe_version)
        self.assertIsNone(video.playback_color_policy)

    def test_nondefault_nested_rendition_directory_matches_configured_resolver(self):
        subdir="encoded/normalized-public"
        (self.root/"encoded").mkdir()
        (self.root/"transcoded").rename(self.root/subdir)
        self.relative=subdir+"/"+self.name;self.path=self.root/self.relative
        self.env["transcode_output_dir"]=lambda:self.root/subdir
        self.resolve=playback_resolver(self.root,subdir)
        video=self.run_import()
        self.assertEqual(video.playback_storage_key,self.name)
        self.assertEqual(self.resolve(video),self.path)

    def test_fixed_legacy_name_in_nested_directory_still_uses_weak_fallback(self):
        subdir="encoded/ready"
        self.env["transcode_output_dir"]=lambda:self.root/subdir
        self.resolve=playback_resolver(self.root,subdir)
        video=self.run_import(f"{subdir}/{self.video_id}.mp4")
        self.assertIsNone(video.playback_storage_key)
        self.assertEqual(self.resolve(video),self.root/subdir/f"{self.video_id}.mp4")

    def test_media_root_itself_can_remain_the_configured_rendition_directory(self):
        self.path.rename(self.root/self.name);self.path=self.root/self.name;self.relative=self.name
        self.env["transcode_output_dir"]=lambda:self.root
        self.resolve=playback_resolver(self.root,"")
        video=self.run_import()
        self.assertEqual(self.resolve(video),self.path)

    def test_unsafe_configured_directory_is_rejected_before_content_read(self):
        self.env["build_media_inventory"]=lambda *args:self.fail("Invalid configured paths must not be read.")
        for directory in (self.root.parent/"outside",self.root/".."/"outside",self.root/"bad\\folder",self.root/"bad\nfolder"):
            self.env["transcode_output_dir"]=lambda value=directory:value
            with self.subTest(directory=directory),self.assertRaisesRegex(ValueError,"inside the configured media root"):
                self.run_import()

    def test_fixed_legacy_path_keeps_nullable_identity_and_missing_file_compatibility(self):
        relative = f"transcoded/{self.video_id}.mp4"
        video = self.run_import(relative)
        self.assertIsNone(video.playback_storage_key)
        self.assertIsNone(video.playback_sha256_checksum)
        self.assertIsNone(video.playback_file_size_bytes)
        self.assertEqual(self.resolve(video), self.root / relative)
        self.assertFalse((self.root / relative).exists())

    def test_existing_optional_fixed_path_inventory_behavior_is_preserved(self):
        relative = f"transcoded/{self.video_id}.mp4"
        video = self.run_import(relative, playback_inventory={str(self.video_id): {"sha256": self.sha, "size_bytes": len(self.data)}})
        self.assertIsNone(video.playback_storage_key)
        self.assertEqual(video.playback_sha256_checksum, self.sha)
        self.assertEqual(video.playback_file_size_bytes, len(self.data))

    def test_missing_or_corrupt_versioned_file_cannot_be_labeled_as_verified(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "unavailable or changed"): self.run_import()
        self.path.write_bytes(b"x" * len(self.data))
        with self.assertRaisesRegex(ValueError, "differ from their named identity"): self.run_import()

    def test_same_size_changed_bytes_with_restored_mtime_are_rejected(self):
        before = self.path.stat()
        self.path.write_bytes(b"x" * len(self.data))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "differ from their named identity"): self.run_import()

    def test_foreign_video_digest_nonce_and_path_shapes_are_rejected_before_content_read(self):
        self.env["build_media_inventory"] = lambda *args: self.fail("Invalid paths must not be read.")
        for relative in (f"transcoded/{uuid.uuid4()}.{self.sha}.{'e' * 32}.mp4",
            f"transcoded/{self.video_id}.{self.sha}.{'e' * 31}.mp4",
            f"transcoded/{self.video_id}.{'A' * 64}.{'e' * 32}.mp4",
            "transcoded/../private.mp4", "/absolute/generated.mp4", "other/generated.mp4",
            "transcoded/nested/" + self.name, "transcoded/" + self.name + "\n"):
            with self.subTest(relative=relative), self.assertRaises(ValueError): self.run_import(relative)

    def test_file_symlink_is_rejected_even_with_matching_target_bytes(self):
        self.path.unlink()
        target = self.root / "generated-target.mp4"
        target.write_bytes(self.data)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "unavailable or changed"): self.run_import()

    def test_transcoded_ancestor_symlink_is_rejected(self):
        (self.root / "transcoded").rename(self.root / "generated-elsewhere")
        (self.root / "transcoded").symlink_to(self.root / "generated-elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unavailable or changed"): self.run_import()

    def test_reimport_replaces_old_generation_identity_with_the_legacy_versioned_identity(self):
        self.existing = self.video_class(id=self.video_id, publication_generation_id=uuid.uuid4(),
            playback_storage_key="old", playback_sha256_checksum="0" * 64, playback_file_size_bytes=1)
        video = self.run_import()
        self.assertIsNone(video.publication_generation_id)
        self.assertEqual(video.playback_storage_key, self.name)
        self.assertEqual(self.resolve(video), self.path)

    def test_generation_import_never_uses_legacy_path_inference_or_file_hashing(self):
        generation = str(uuid.uuid4())
        relative = f".loci-publication-generations/{generation}/files/{self.relative}"
        self.env["_legacy_playback_identity"] = lambda *args: self.fail("Generation import must remain on its bound inventory path.")
        video = self.run_import(relative, generation_id=generation,
            playback_inventory={str(self.video_id): {"sha256": self.sha, "size_bytes": len(self.data)}})
        self.assertEqual(video.publication_generation_id, uuid.UUID(generation))
        self.assertIsNone(video.playback_storage_key)
        self.assertEqual(video.playback_sha256_checksum, self.sha)
        self.assertEqual(video.playback_file_size_bytes, len(self.data))
        self.assertEqual(video.playback_recipe_version, "publication-generation-v1")
        self.assertEqual(video.publication_attribution_json, [])

    def test_generation_import_still_refuses_missing_bound_playback_inventory(self):
        with self.assertRaisesRegex(ValueError, "no playback byte identity"):
            self.run_import(generation_id=str(uuid.uuid4()), playback_inventory={})

    def test_new_legacy_projection_preserves_present_empty_snapshot_without_live_fallback_marker(self):
        video = self.run_import()
        self.assertIsNone(video.publication_generation_id)
        self.assertEqual(video.publication_attribution_json, [])

    def test_true_old_legacy_projection_missing_field_retains_null_live_semantics(self):
        body = self.manifest(); body["videos"][0].pop("publication_attribution_json")
        video = self.run_payload(body)
        self.assertIsNone(video.publication_generation_id)
        self.assertIsNone(video.publication_attribution_json)

    def test_present_null_or_private_attribution_is_rejected_before_owner_flush(self):
        self.env["_ensure_public_owner"] = lambda db: self.fail("Invalid attribution cannot reach the first write.")
        for value in (None, [{"raw_text_observed": "GENERATED_PRIVATE_MARKER"}]):
            body = self.manifest(); body["videos"][0]["publication_attribution_json"] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.run_payload(body)

    def test_generation_missing_snapshot_is_rejected_without_legacy_reconstruction(self):
        self.env["_ensure_public_owner"] = lambda db: self.fail("Missing generation data cannot reach writes.")
        body = self.manifest(); body["videos"][0].pop("publication_attribution_json")
        with self.assertRaises(ValueError):
            self.run_payload(body, generation_id=str(uuid.uuid4()),
                playback_inventory={str(self.video_id): {"sha256": self.sha, "size_bytes": len(self.data)}})

    def test_imported_public_attribution_is_copied_and_date_canonicalized(self):
        snapshot = [{"start_ms": 0, "end_ms": None, "payload": {
            "speaker_label": "Generated approved label", "session_date": datetime(2026, 1, 2).date(),
            "session_date_text": "January 2, 2026", "session_date_precision": "day", "attribution_mode": "named"}}]
        body = self.manifest(); body["videos"][0]["publication_attribution_json"] = snapshot
        video = self.run_payload(body)
        self.assertEqual(video.publication_attribution_json[0]["payload"]["session_date"], "2026-01-02")
        snapshot[0]["payload"]["speaker_label"] = "Changed source dictionary"
        self.assertEqual(video.publication_attribution_json[0]["payload"]["speaker_label"], "Generated approved label")


if __name__ == "__main__": unittest.main()
