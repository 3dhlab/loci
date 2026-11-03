"""Real generated files exercise selection sealing and immutable installation."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from uuid import uuid4
import ast
import re
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.publication_generation_files import (
    generation_manifest, install_generation, selected_metadata,
)
from app.services.publication_selection_store import (
    PRIVATE_SELECTIONS, SelectionError, file_digest, load_selection, seal_selection,
)
from app.services.publication_readback import digest


class GenerationFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-generated-selection-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "models").mkdir()
        (self.source / "models/generated.glb").write_bytes(b"generated-model-bytes")
        self.manifest = {"media_paths": ["models/generated.glb"], "object_models": []}
        self.ident = str(uuid4())
        self.seal = seal_selection(self.source, self.ident, self.manifest,
                                   self.manifest["media_paths"], "a" * 64, max_bytes=4096)

    def load(self, seal=None):
        seal = seal or self.seal
        return load_selection(self.source, self.ident, seal["selection_ref"],
                              seal["selection_digest"], seal["metadata"])

    def install(self, seal=None, bundle=None):
        seal = seal or self.seal
        return install_generation(self.target, self.ident, seal["selection_digest"],
                                  seal["metadata"], bundle or self.load()["bundle"])

    def test_source_edit_after_admission_does_not_reselect_generation(self):
        (self.source / "models/generated.glb").write_bytes(b"changed live source")
        installed = self.install()
        self.assertEqual((installed / "models/generated.glb").read_bytes(), b"generated-model-bytes")

    def test_bundle_and_seal_read_only_and_reloadable_after_process_scope(self):
        selected = self.load()
        self.assertEqual(selected["bundle"].stat().st_mode & 0o777, 0o400)
        self.assertEqual(selected["bundle"].parent.stat().st_mode & 0o777, 0o500)
        self.assertEqual(self.load()["seal"], self.seal)

    def test_target_archive_consumes_verified_descriptor_not_a_reopened_path(self):
        bundle = self.load()["bundle"]
        original_open = tarfile.open
        consumed = []
        def replace_after_hash(*args, **kwargs):
            self.assertEqual(args, ())
            supplied = kwargs["fileobj"]
            self.assertFalse(supplied.closed)
            consumed.append(supplied.fileno())
            os.chmod(bundle.parent, 0o700)
            bundle.rename(bundle.with_name("retained-original.tar.gz"))
            bundle.write_bytes(b"replacement is not an archive")
            return original_open(*args, **kwargs)
        # Renaming the still-open original may update ctime and safely reject.
        # It must never parse the replacement pathname or block reopening it.
        with patch("app.services.publication_generation_files.tarfile.open", side_effect=replace_after_hash):
            try:
                result = self.install(bundle=bundle)
            except SelectionError as error:
                self.assertIn("descriptor changed", str(error))
            else:
                self.assertEqual((result / "models/generated.glb").read_bytes(), b"generated-model-bytes")
        self.assertEqual(len(consumed), 1)

    def test_target_refuses_nonregular_input_before_archive_parser(self):
        with patch("app.services.publication_generation_files.tarfile.open") as parser:
            with self.assertRaises((SelectionError, OSError)):
                self.install(bundle=self.root)
            parser.assert_not_called()

    def test_existing_unbound_selection_is_not_adopted(self):
        with self.assertRaises(FileExistsError):
            seal_selection(self.source, self.ident, self.manifest, self.manifest["media_paths"],
                           "a" * 64, max_bytes=4096)

    def test_changed_metadata_ref_or_digest_rejected(self):
        changed = deepcopy(self.seal)
        changed["metadata"]["manifest"]["generated_at"] = "changed"
        for seal in (changed, {**self.seal, "selection_digest": "b" * 64},
                     {**self.seal, "selection_ref": str(uuid4())}):
            with self.subTest(seal=seal), self.assertRaises(SelectionError):
                self.load(seal)

    def test_changed_bundle_rejected_before_target_files_created(self):
        bundle = self.load()["bundle"]
        os.chmod(bundle, 0o600)
        bundle.write_bytes(b"tampered")
        with self.assertRaises(SelectionError):
            self.load()
        with self.assertRaises(SelectionError):
            self.install(bundle=bundle)
        self.assertFalse((self.target / ".loci-publication-generations").exists())

    def test_symlink_selection_ancestor_rejected(self):
        base = self.source / PRIVATE_SELECTIONS
        moved = base.with_name("retained-original-selection")
        base.rename(moved)
        base.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.load()

    def test_foreign_or_shared_private_directory_rejected(self):
        os.chmod(self.source / PRIVATE_SELECTIONS, 0o755)
        with self.assertRaises(SelectionError):
            self.load()

    def test_capture_byte_allowance_checked_before_new_operation(self):
        ident = str(uuid4())
        with self.assertRaises(SelectionError):
            seal_selection(self.source, ident, self.manifest, self.manifest["media_paths"],
                           "a" * 64, max_bytes=1)
        self.assertFalse((self.source / PRIVATE_SELECTIONS / ident).exists())

    def test_duplicate_paths_and_reserved_private_selection_refused(self):
        for paths in (["models/generated.glb"] * 2,
                      [f"{PRIVATE_SELECTIONS}/{self.ident}/media.tar.gz"]):
            with self.assertRaises(SelectionError):
                seal_selection(self.source, str(uuid4()), {"media_paths": paths}, paths,
                               "a" * 64, max_bytes=4096)

    def test_installed_files_immutable_and_previous_generation_retained(self):
        installed = self.install()
        self.assertEqual((installed / "models/generated.glb").stat().st_mode & 0o777, 0o400)
        with self.assertRaises(FileExistsError):
            self.install()
        self.assertEqual((installed / "models/generated.glb").read_bytes(), b"generated-model-bytes")

    def bad_bundle(self, members):
        path = self.root / "bad.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for member, data in members:
                archive.addfile(member, io.BytesIO(data) if data is not None else None)
        metadata = deepcopy(self.seal["metadata"])
        metadata["bundle_sha256"], metadata["bundle_bytes"] = file_digest(path)
        return path, {**self.seal, "metadata": metadata, "selection_digest": digest(metadata)}

    def test_archive_traversal_symlink_and_unknown_member_refused(self):
        cases = []
        for name in ("../outside", "/absolute", "unknown", "models/generated.glb"):
            item = tarfile.TarInfo(name)
            if name == "models/generated.glb":
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
            cases.append([(item, None)])
        for members in cases:
            with self.subTest(members=members):
                self.ident = str(uuid4())
                path, seal = self.bad_bundle(members)
                with self.assertRaises(SelectionError):
                    self.install(seal, path)
                self.assertFalse((self.root / "outside").exists())
                path.unlink()

    def test_duplicate_or_directory_member_refused(self):
        data = b"generated-model-bytes"
        regular = tarfile.TarInfo("models/generated.glb")
        regular.size = len(data)
        directory = tarfile.TarInfo("models")
        directory.type = tarfile.DIRTYPE
        for members in ([(regular, data), (regular, data)], [(directory, None)]):
            self.ident = str(uuid4())
            path, seal = self.bad_bundle(members)
            with self.assertRaises(SelectionError):
                self.install(seal, path)
            path.unlink()

    def test_oversize_pax_metadata_rejected_before_large_parser_allocation(self):
        item = tarfile.TarInfo("oversize-pax")
        item.type = tarfile.XHDTYPE
        item.size = 65537
        path, seal = self.bad_bundle([(item, b"x" * item.size)])
        with self.assertRaisesRegex(SelectionError, "metadata is too large"):
            self.install(seal, path)

    def test_selected_model_and_persisted_playback_must_match_approved_identity(self):
        for manifest in (
            {**self.manifest, "object_models": [{"id": "model", "public_media_relpath": "models/generated.glb",
               "sha256_checksum": "a" * 64, "file_size_bytes": 21}]},
            {**self.manifest, "videos": [{"id": "video", "public_media_relpath": "models/generated.glb"}],
             "playback_identities": {"video": {"sha256": "a" * 64, "size_bytes": 21}}},
        ):
            with self.subTest(manifest=manifest), self.assertRaises(SelectionError):
                seal_selection(self.source, str(uuid4()), manifest, manifest["media_paths"], "a" * 64, max_bytes=4096)


    def test_missing_and_wrong_member_content_refused(self):
        for data in (b"", b"different-model-bytes"):
            self.ident = str(uuid4())
            item = tarfile.TarInfo("models/generated.glb")
            item.size = len(data)
            path, seal = self.bad_bundle([(item, data)] if data else [])
            with self.assertRaises(SelectionError):
                self.install(seal, path)
            path.unlink()

    def test_generation_paths_hydrate_copy_without_mutating_selected_manifest(self):
        manifest = {"media_paths": ["x.glb"], "object_models": [{"public_media_relpath": "x.glb"}],
                    "clips": [{"public_poster_relpath": "poster.png"}],
                    "visual_window_descriptions": [{"frame_manifest_json": {"thumbnail": {"path": "frame.png"}}}]}
        before = deepcopy(manifest)
        result = generation_manifest(manifest, self.ident)
        self.assertEqual(manifest, before)
        self.assertIn(self.ident, result["object_models"][0]["public_media_relpath"])
        self.assertIn(self.ident, result["clips"][0]["public_poster_relpath"])
        self.assertIn(self.ident, result["visual_window_descriptions"][0]["frame_manifest_json"]["thumbnail"]["path"])


class GenerationPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-generation-playback-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        path = Path(__file__).parents[1] / "app/services/media_transcode.py"
        tree = ast.parse(path.read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"transcode_output_dir", "transcode_output_path", "video_playback_path"}]
        self.env = {"Path": Path, "re": re, "settings": SimpleNamespace(media_root=str(self.root), transcode_output_subdir="transcoded")}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.env)
        self.resolve = self.env["video_playback_path"]
        self.ident = str(uuid4())
        self.video = SimpleNamespace(id=uuid4(), publication_generation_id=self.ident,
            playback_storage_key=None, playback_sha256_checksum="a" * 64, playback_file_size_bytes=4)
        self.path = self.root / ".loci-publication-generations" / self.ident / "files/transcoded/generated.mp4"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"data")
        self.video.source_path = str(self.path)

    def test_explicit_generation_resolves_exact_frozen_video(self):
        self.assertEqual(self.resolve(self.video), self.path)

    def test_generation_cannot_use_other_generation_original_upload_or_symlink(self):
        outside = self.root / "private-original.mp4"
        outside.write_bytes(b"data")
        for value in (str(outside), str(self.path).replace(self.ident, str(uuid4())), "../private-original.mp4"):
            self.video.source_path = value
            self.assertIsNone(self.resolve(self.video))
        self.video.source_path = str(self.path)
        self.path.unlink()
        self.path.symlink_to(outside)
        self.assertIsNone(self.resolve(self.video))

    def test_generation_requires_validated_sha_size_and_uuid(self):
        for field, value in (("publication_generation_id", "not-a-uuid"),
                             ("playback_sha256_checksum", None), ("playback_file_size_bytes", True)):
            original = getattr(self.video, field)
            setattr(self.video, field, value)
            self.assertIsNone(self.resolve(self.video))
            setattr(self.video, field, original)

    def test_ordinary_authoring_never_falls_back_to_raw_upload(self):
        self.video.publication_generation_id = None
        expected = self.root / "transcoded" / f"{self.video.id}.mp4"
        self.assertEqual(self.resolve(self.video), expected)
        self.video.playback_storage_key = "../../private-original.mp4"
        self.assertIsNone(self.resolve(self.video))




if __name__ == "__main__":
    unittest.main()
