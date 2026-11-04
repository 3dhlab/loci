"""Generated record/file identities only; no DB, remote target or collection data."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import sys
import unittest
from uuid import UUID

from app.services.publication_readback import (
    COLLECTIONS, PATH_FIELDS, PublicationReadbackError, bounded_readback_command, canonical, expected_receipt,
    media_contract, observe_receipt, record_contract, require_receipt,
)


class PublicationReadbackTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {name: [] for name in COLLECTIONS}
        self.manifest.update(generated_at="2026-01-01T00:00:00Z", media_paths=["models/generated.glb"])
        self.manifest["objects"] = [{"id": "00000000-0000-0000-0000-000000000001",
            "website_object_id": "generated-object", "is_published": True}]
        self.manifest["object_models"] = [{"id": "00000000-0000-0000-0000-000000000002",
            "public_media_relpath": "models/generated.glb", "position_x": 1.25,
            "created_at": "2026-01-01T00:00:00Z"}]
        self.manifest["object_model_annotations"] = [{"id": "00000000-0000-0000-0000-000000000003",
            "title": "Generated interpretation", "playlist_json": [{"video_id": "generated-video", "start_ms": 100}],
            "website_annotation_id": "generated-annotation", "website_relations_json": None}]
        self.manifest["clips"] = [{"id": "00000000-0000-0000-0000-000000000004",
            "citation_text": "Generated citation", "website_clip_id": "generated-clip"}]
        self.inventory = {"models/generated.glb": {"size_bytes": 9, "mtime_ns": 17,
            "sha256": hashlib.sha256(b"generated").hexdigest()}}
        self.rows = {}
        for name in COLLECTIONS:
            self.rows[name] = {}
            for row in self.manifest[name]:
                value = deepcopy(row)
                for field, attribute in PATH_FIELDS.get(name, {}).items():
                    if field in value:
                        path = value.pop(field)
                        value[attribute] = "/media/" + path if path is not None else None
                self.rows[name][row["id"]] = value

    def observe(self, inventory=None):
        return observe_receipt(self.manifest, self.inventory, self.rows,
            self.inventory if inventory is None else inventory, media_root="/media")

    def test_exact_generated_records_annotations_citations_and_bytes(self):
        self.assertEqual(self.observe(), expected_receipt(self.manifest, self.inventory))

    def test_empty_record_only_package(self):
        manifest = {"media_paths": []}
        self.assertEqual(observe_receipt(manifest, {}, {}, {}, media_root="/absent")["media_file_count"], 0)

    def test_imported_uuid_datetime_and_decimal_are_normalized(self):
        row = self.rows["object_models"][self.manifest["object_models"][0]["id"]]
        row.update(id=UUID(row["id"]), position_x=Decimal("1.250000"),
            created_at=datetime(2025,12,31,18,tzinfo=timezone(timedelta(hours=-6))))
        self.observe()

    def test_six_decimal_schema_scale(self):
        self.manifest["object_models"][0]["position_x"] = 1.25000001
        self.rows["object_models"][self.manifest["object_models"][0]["id"]]["position_x"] = Decimal("1.250000")
        self.observe()

    def test_numeric_ties_follow_postgresql_away_from_zero(self):
        for field, source, target in (("position_x",0.0000005,"0.000001"),
                                      ("position_x",-0.0000005,"-0.000001"),
                                      ("corpus_density",0.00005,"0.0001"),
                                      ("corpus_density",-0.00005,"-0.0001")):
            manifest={"objects":[{"id":"tie",field:source}]}
            observed={"objects":{"tie":{"id":"tie",field:Decimal(target)}}}
            self.assertEqual(record_contract(manifest),record_contract(manifest,observed,media_root="/media"))

    def test_real_numeric_change_rejected(self):
        self.rows["object_models"][self.manifest["object_models"][0]["id"]]["position_x"] = Decimal("1.250001")
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_annotation_title_playlist_and_stable_id_changes_rejected(self):
        for field, value in (("title", "changed"), ("playlist_json", []), ("website_annotation_id", "other")):
            with self.subTest(field=field):
                rows = deepcopy(self.rows)
                rows["object_model_annotations"][self.manifest["object_model_annotations"][0]["id"]][field] = value
                with self.assertRaises(PublicationReadbackError):
                    observe_receipt(self.manifest,self.inventory,rows,self.inventory,media_root="/media")

    def test_citation_or_stable_clip_identity_changes_rejected(self):
        for field in ("citation_text", "website_clip_id"):
            with self.subTest(field=field):
                rows = deepcopy(self.rows)
                rows["clips"][self.manifest["clips"][0]["id"]][field] = "changed"
                with self.assertRaises(PublicationReadbackError):
                    observe_receipt(self.manifest,self.inventory,rows,self.inventory,media_root="/media")

    def test_object_citation_slug_or_visibility_change_rejected(self):
        for field,value in (("website_object_id","other"),("is_published",False)):
            rows=deepcopy(self.rows); rows["objects"][self.manifest["objects"][0]["id"]][field]=value
            with self.assertRaises(PublicationReadbackError):
                observe_receipt(self.manifest,self.inventory,rows,self.inventory,media_root="/media")

    def test_missing_row_rejected(self):
        self.rows["clips"] = {}
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_extra_destination_row_rejected(self):
        self.rows["objects"]["unexpected"] = {"id": "unexpected"}
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_missing_imported_field_rejected(self):
        del self.rows["clips"][self.manifest["clips"][0]["id"]]["citation_text"]
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_invalid_observed_collection_cannot_disable_checks(self):
        self.rows["clips"] = None
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_duplicated_source_row_rejected(self):
        self.manifest["objects"] *= 2
        with self.assertRaises(PublicationReadbackError): self.observe()

    def test_media_same_size_corruption_rejected(self):
        inventory=deepcopy(self.inventory);inventory["models/generated.glb"]["sha256"]="0"*64
        with self.assertRaises(PublicationReadbackError): self.observe(inventory)

    def test_media_size_mismatch_rejected(self):
        inventory=deepcopy(self.inventory);inventory["models/generated.glb"]["size_bytes"]+=1
        with self.assertRaises(PublicationReadbackError): self.observe(inventory)

    def test_media_mtime_alone_is_not_content_identity(self):
        inventory=deepcopy(self.inventory);inventory["models/generated.glb"]["mtime_ns"]+=1
        self.assertEqual(self.observe(inventory), self.observe())

    def test_extra_missing_media_rejected(self):
        for inventory in ({}, {**self.inventory,"extra":next(iter(self.inventory.values()))}):
            with self.subTest(inventory=inventory),self.assertRaises(PublicationReadbackError): self.observe(inventory)

    def test_invalid_media_path_hash_and_size_rejected(self):
        for path in ("../private","/absolute","a//b","a\\b","a\n"):
            with self.subTest(path=path),self.assertRaises(PublicationReadbackError):
                media_contract({path:next(iter(self.inventory.values()))})
        for field,value in (("sha256","A"*64),("sha256","bad"),("size_bytes",True),("size_bytes",-1)):
            item=deepcopy(next(iter(self.inventory.values())));item[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(PublicationReadbackError): media_contract({"valid":item})

    def test_manifest_media_selection_must_match(self):
        for paths in ([],["models/generated.glb"]*2, [["invalid"]]):
            self.manifest["media_paths"]=paths
            with self.assertRaises(PublicationReadbackError): expected_receipt(self.manifest,self.inventory)

    def test_destination_path_escape_rejected(self):
        for path in ("/private/object.glb","/media/../private/object.glb","relative"):
            self.rows["object_models"][self.manifest["object_models"][0]["id"]]["storage_path"]=path
            with self.assertRaises(PublicationReadbackError): self.observe()

    def test_frame_paths_are_dehydrated(self):
        manifest={"media_paths":[], "visual_window_descriptions":[{"id":"frame","frame_manifest_json":{
            "thumbnail":{"path":"frames/generated.png"},"sample_frames":[{"path":"frames/sample.png"}]}}]}
        observed={"visual_window_descriptions":{"frame":{"id":"frame","frame_manifest_json":{
            "thumbnail":{"path":"/media/frames/generated.png"},"sample_frames":[{"path":"/media/frames/sample.png"}]}}}}
        self.assertEqual(record_contract(manifest),record_contract(manifest,observed,media_root="/media"))

    def test_embedding_vectors_are_explicitly_outside_receipt(self):
        self.manifest["objects"][0]["embedding_vector"]=[1,2]
        self.assertEqual(self.observe(),expected_receipt(self.manifest,self.inventory))

    def test_rederived_corpus_timestamps_are_outside_content_identity(self):
        manifest={"corpus_phrases":[{"id":"phrase","phrase_text":"generated",
            "created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}]}
        observed={"corpus_phrases":{"phrase":{"id":"phrase","phrase_text":"generated",
            "created_at":"2025-01-01T00:00:00Z","updated_at":"2025-01-01T00:00:00Z"}}}
        self.assertEqual(record_contract(manifest),record_contract(manifest,observed,media_root="/media"))

    def test_receipt_requires_fresh_challenge_exact_schema_and_content(self):
        expected=self.observe();challenge="a"*64
        self.assertEqual(require_receipt({"receipt":expected,"challenge":challenge},expected,challenge),expected)
        for payload in ({"receipt":expected,"challenge":"b"*64}, {"receipt":expected},
                        {"receipt":{**expected,"media_file_count":8},"challenge":challenge},
                        {"receipt":{**expected,"version":True},"challenge":challenge},
                        {"receipt":expected,"challenge":challenge,"extra":True}):
            with self.subTest(payload=payload),self.assertRaises(PublicationReadbackError): require_receipt(payload,expected,challenge)

    def test_nonfinite_and_naive_timestamps_rejected(self):
        for field,value in (("position_x",float("nan")),("created_at",datetime(2026,1,1))):
            row=deepcopy(self.manifest["object_models"][0]);row[field]=value
            with self.assertRaises(PublicationReadbackError): record_contract({"object_models":[row]})

    def test_source_order_and_generation_time_do_not_change_selected_content(self):
        expected=self.observe()
        self.manifest["generated_at"]="later"
        self.assertEqual(self.observe(),expected)

    def test_real_benign_child_receives_private_stdin(self):
        result=bounded_readback_command([sys.executable,"-I","-c",
            "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"],b"generated",timeout=2)
        self.assertEqual(result,b"detareneg")

    def test_real_benign_child_nonzero_rejected(self):
        with self.assertRaises(ValueError):
            bounded_readback_command([sys.executable,"-I","-c","raise SystemExit(3)"],b"",timeout=2)

    def test_real_benign_child_output_limit_rejected(self):
        with self.assertRaises(ValueError):
            bounded_readback_command([sys.executable,"-I","-c","import sys;sys.stdout.write('x'*65537)"],b"",timeout=2)

    def test_real_benign_child_timeout_reaped(self):
        with self.assertRaises(TimeoutError):
            bounded_readback_command([sys.executable,"-I","-c","import time;time.sleep(2)"],b"",timeout=.05)


if __name__ == "__main__": unittest.main()
