"""Generated public attribution snapshots; no database or live collection data."""
import ast
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from app.services.publication_attribution import (
    MAX_ATTRIBUTION_BANDS, MAX_ATTRIBUTION_TIME_MS,
    PublicAttributionError, validate_public_attribution_bands,
)


def payload(speaker="Generated public label", exact_date=date(2026, 1, 2)):
    return {"speaker_label": speaker, "session_date": exact_date,
        "session_date_text": "January 2, 2026", "session_date_precision": "day",
        "attribution_mode": "named" if speaker else "unavailable"}


def bands():
    return [{"start_ms": 0, "end_ms": None, "payload": payload()}]


def public_evidence_functions():
    path = Path(__file__).parents[1] / "app/services/public_evidence.py"
    names = {"_citation_attribution_bands", "_citation_attribution_payload", "_public_title_card_speaker_label",
        "_title_card_observation_has_reliable_date", "_session_date_precision", "_normalized_confidence_value",
        "_optional_nonempty_string", "PublicEvidenceConflictError"}
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    assert {node.name for node in nodes} == names
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    env = {"validate_public_attribution_bands": validate_public_attribution_bands,
        "_RELIABLE_TITLE_CARD_CONFIDENCES": {"high", "medium"}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), str(path), "exec"), env)
    return env


class PublicAttributionContractTests(unittest.TestCase):
    def test_date_objects_normalize_to_json_date_without_private_fields(self):
        frozen = validate_public_attribution_bands(bands())
        self.assertEqual(frozen[0]["payload"]["session_date"], "2026-01-02")
        self.assertEqual(validate_public_attribution_bands(json.loads(json.dumps(frozen))), frozen)

    def test_empty_snapshot_is_deliberate_and_null_is_not_a_generation_snapshot(self):
        self.assertEqual(validate_public_attribution_bands([]), [])
        with self.assertRaises(PublicAttributionError): validate_public_attribution_bands(None)

    def test_all_output_containers_are_copied(self):
        original = bands(); frozen = validate_public_attribution_bands(original)
        frozen[0]["payload"]["speaker_label"] = "Changed returned copy"
        self.assertEqual(original[0]["payload"]["speaker_label"], "Generated public label")

    def test_raw_ocr_presenter_lineage_and_paths_cannot_enter_public_payload(self):
        for field in ("presenter_name", "presenter_age", "raw_text_observed", "raw_payload_json",
                      "additional_text", "model_run_id", "source_path", "source_version"):
            value = bands(); value[0]["payload"][field] = "GENERATED_PRIVATE_MARKER"
            with self.subTest(field=field), self.assertRaises(PublicAttributionError) as error:
                validate_public_attribution_bands(value)
            self.assertNotIn("GENERATED_PRIVATE_MARKER", str(error.exception))
        value = bands(); value[0]["raw_payload_json"] = {}
        with self.assertRaises(PublicAttributionError): validate_public_attribution_bands(value)

    def test_band_count_is_bounded_before_iteration(self):
        with self.assertRaises(PublicAttributionError):
            validate_public_attribution_bands([bands()[0]] * (MAX_ATTRIBUTION_BANDS + 1))

    def test_strings_are_typed_bounded_canonical_and_control_free(self):
        for text in ("", " x", "x ", "x\n", "x\x00", "x" * 256, 7, True):
            value = bands(); value[0]["payload"]["speaker_label"] = text
            with self.subTest(text=repr(text)), self.assertRaises(PublicAttributionError): validate_public_attribution_bands(value)
        value = bands(); value[0]["payload"]["speaker_label"] = "Generated Unicode étiquette"
        self.assertEqual(validate_public_attribution_bands(value)[0]["payload"]["speaker_label"], "Generated Unicode étiquette")

    def test_date_format_and_calendar_are_strict(self):
        for value in ("2026-02-30", "2026-1-2", "2026-01-02T00:00:00Z", "26-01-02", True,
                      datetime(2026, 1, 2, tzinfo=timezone.utc)):
            snapshot = bands(); snapshot[0]["payload"]["session_date"] = value
            with self.subTest(value=value), self.assertRaises(PublicAttributionError): validate_public_attribution_bands(snapshot)

    def test_policy_flags_do_not_smuggle_a_label_into_unavailable_attribution(self):
        for field, value in (("attribution_mode", "unavailable"), ("session_date_precision", "text")):
            snapshot = bands(); snapshot[0]["payload"][field] = value
            with self.assertRaises(PublicAttributionError): validate_public_attribution_bands(snapshot)
        snapshot = bands(); snapshot[0]["payload"].update(session_date=None, session_date_text=None, session_date_precision="unknown")
        with self.assertRaises(PublicAttributionError): validate_public_attribution_bands(snapshot)

    def test_date_only_and_suppressed_gap_payloads_remain_valid(self):
        value = bands(); value[0]["payload"] = payload(speaker=None)
        validate_public_attribution_bands(value)
        value[0]["payload"].update(session_date=None, session_date_text=None, session_date_precision="unknown")
        validate_public_attribution_bands(value)

    def test_timing_is_typed_ordered_bounded_and_never_opens_before_zero(self):
        for start, end in ((True, None), (-1, None), (1, None), (0, 0), (0, True),
                           (0, MAX_ATTRIBUTION_TIME_MS + 1)):
            value = bands(); value[0].update(start_ms=start, end_ms=end)
            with self.subTest(start=start, end=end), self.assertRaises(PublicAttributionError): validate_public_attribution_bands(value)
        value = [{"start_ms":0,"end_ms":None,"payload":payload()}, {"start_ms":100,"end_ms":None,"payload":payload()}]
        with self.assertRaises(PublicAttributionError): validate_public_attribution_bands(value)

    def test_existing_tiny_boundary_overlap_order_is_preserved_without_inference(self):
        value = [{"start_ms":0,"end_ms":1,"payload":payload()},
                 {"start_ms":0,"end_ms":None,"payload":payload(speaker="Generated second label")}]
        self.assertEqual([band["start_ms"] for band in validate_public_attribution_bands(value)], [0,0])


class FrozenAttributionReaderTests(unittest.TestCase):
    def setUp(self):
        self.env = public_evidence_functions()
        self.video = SimpleNamespace(id=uuid.uuid4(), publication_generation_id=uuid.uuid4(),
            publication_attribution_json=validate_public_attribution_bands(bands()))
        self.env["_public_title_card_observations_for_video"] = lambda *args: self.fail("A frozen projection must never read stale title cards.")

    def read(self): return self.env["_citation_attribution_bands"](object(), self.video)

    def test_generation_reads_only_its_validated_snapshot(self):
        self.assertEqual(self.read(), self.video.publication_attribution_json)

    def test_generation_empty_snapshot_clears_old_target_speaker_and_date(self):
        self.video.publication_attribution_json = []
        self.assertEqual(self.read(), [])

    def test_missing_or_malformed_generation_snapshot_fails_without_stale_fallback(self):
        for value in (None, {}, [{"raw_text_observed":"GENERATED_PRIVATE"}]):
            self.video.publication_attribution_json = value
            with self.subTest(value=value), self.assertRaises(self.env["PublicEvidenceConflictError"]): self.read()

    def test_new_legacy_projection_with_present_snapshot_never_falls_back(self):
        self.video.publication_generation_id = None
        self.assertEqual(self.read(), self.video.publication_attribution_json)
        self.video.publication_attribution_json = []
        self.assertEqual(self.read(), [])

    def test_genuine_authoring_or_old_legacy_null_snapshot_keeps_live_policy(self):
        self.video.publication_generation_id = None; self.video.publication_attribution_json = None
        observed = []
        self.env["_public_title_card_observations_for_video"] = lambda db, ident: observed.append(ident) or []
        self.assertEqual(self.read(), []); self.assertEqual(observed, [self.video.id])

    def test_existing_public_policy_projects_explicit_public_label_not_private_presenter(self):
        observation = SimpleNamespace(title_card_visible=True, public_speaker_label="Generated approved public label",
            session_date=date(2026,1,2), session_date_text="January 2, 2026",
            confidence_session_date="high", confidence_presenter="high",
            presenter_name="GENERATED_PRIVATE_PERSON", raw_text_observed="GENERATED_PRIVATE_OCR",
            raw_payload_json={"private":"GENERATED_PRIVATE_PAYLOAD"})
        projected = self.env["_citation_attribution_payload"](observation)
        frozen = validate_public_attribution_bands([{"start_ms":0,"end_ms":None,"payload":projected}])
        self.assertNotIn("GENERATED_PRIVATE", json.dumps(frozen))
        observation.public_speaker_label = None
        self.assertIsNone(self.env["_citation_attribution_payload"](observation)["speaker_label"])


if __name__ == "__main__": unittest.main()
