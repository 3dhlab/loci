"""Generated selected clip text, real file identities and sealed capture checks."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.services.publication_clip_text import MAX_PUBLIC_CLIP_TEXT_BYTES, selected_clip_excerpts
from app.services.publication_media_inventory import MediaInventoryError, build_media_inventory
from app.services.publication_selection_store import seal_selection


class SelectedClipTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-selected-clip-text-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / "generated.txt"
        self.file.write_text("  Generated public transcript étiquette\n", encoding="utf-8")
        self.manifest = {"media_paths": ["generated.txt"], "clips": [{"id": "clip", "transcript_excerpt": None,
                         "public_transcript_relpath": "generated.txt"}]}
        self.inventory = build_media_inventory(self.root, ["generated.txt"])

    def read(self): return selected_clip_excerpts(self.root, self.manifest, self.inventory)

    def test_fallback_matches_public_utf8_trim_without_modifying_manifest(self):
        original = deepcopy(self.manifest)
        self.assertEqual(self.read(), {"clip": "Generated public transcript étiquette"})
        self.assertEqual(self.manifest, original)

    def test_explicit_excerpt_wins_without_opening_fallback_file(self):
        self.manifest["clips"][0]["transcript_excerpt"] = "  Explicit selected excerpt\n"
        self.file.unlink()
        self.assertEqual(self.read(), {"clip": "Explicit selected excerpt"})

    def test_missing_optional_file_and_empty_public_text_have_null_rendering(self):
        self.file.write_text(" \n")
        self.inventory = build_media_inventory(self.root, ["generated.txt"])
        self.assertEqual(self.read(), {"clip": None})
        self.manifest["clips"][0]["public_transcript_relpath"] = None
        self.assertEqual(self.read(), {"clip": None})

    def test_changed_file_identity_is_rejected(self):
        self.file.write_bytes(b"x" * self.inventory["generated.txt"]["size_bytes"])
        with self.assertRaises(MediaInventoryError): self.read()

    def test_symlink_fallback_is_not_followed(self):
        retained = self.root / "retained-generated.txt"
        self.file.rename(retained); self.file.symlink_to(retained)
        with self.assertRaises(OSError): self.read()

    def test_invalid_utf8_and_size_allowance_are_rejected(self):
        for data in (b"\xff", b"x" * (MAX_PUBLIC_CLIP_TEXT_BYTES + 1)):
            self.file.write_bytes(data)
            self.inventory = build_media_inventory(self.root, ["generated.txt"])
            with self.subTest(size=len(data)), self.assertRaises(MediaInventoryError): self.read()

    def test_combined_excerpt_allowance_is_enforced_during_capture(self):
        self.manifest["clips"] = [{"id": str(i), "transcript_excerpt": "generated"} for i in range(3)]
        with patch("app.services.publication_clip_text.MAX_PUBLIC_CLIP_TEXT_TOTAL_BYTES", 20):
            with self.assertRaisesRegex(MediaInventoryError, "combined allowance"): self.read()

    def test_seal_binds_fallback_text_to_selected_bytes_and_copies_input(self):
        original = deepcopy(self.manifest)
        seal = seal_selection(self.root, str(uuid4()), self.manifest, ["generated.txt"], "a" * 64, max_bytes=4096)
        self.assertEqual(seal["metadata"]["manifest"]["public_clip_excerpts"], self.read())
        self.assertEqual(self.manifest, original)

    def test_change_between_inventory_and_excerpt_is_rejected_before_selection_creation(self):
        self.file.write_bytes(b"x" * self.inventory["generated.txt"]["size_bytes"])
        with patch("app.services.publication_selection_store.build_media_inventory", return_value=self.inventory):
            with self.assertRaises(MediaInventoryError):
                seal_selection(self.root, str(uuid4()), self.manifest, ["generated.txt"], "a" * 64, max_bytes=4096)
        self.assertFalse((self.root / ".loci-publication-selections").exists())


if __name__ == "__main__": unittest.main()
