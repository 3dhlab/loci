"""Real sealed bytes and target bodies with an injected SQL/session boundary.

These regressions do not establish PostgreSQL transactions or SSH acceptance.
"""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import UUID, uuid4

from app.services.publication_generation_files import generation_root, install_generation, selected_metadata
from app.services.publication_job_contract import validate_cancellation_receipt
from app.services.publication_media_inventory import build_media_inventory
from app.services.publication_readback import COLLECTIONS, expected_receipt, observe_receipt
from app.services.publication_selection_store import SelectionError, load_selection, operation_uuid, seal_selection


class DestinationAllowanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-destination-allowance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.target = self.root / "source", self.root / "target"
        self.source.mkdir(); self.target.mkdir()
        self.ident = str(uuid4())
        (self.source / "selected.bin").write_bytes(b"x" * (1536 * 1024))
        self.selected = seal_selection(self.source, self.ident, {"media_paths": ["selected.bin"]},
            ["selected.bin"], "a" * 64, max_bytes=2 * 1024 * 1024)
        self.metadata = self.selected["metadata"]
        self.payload = {"operation_id": self.ident, "selection_digest": self.selected["selection_digest"],
                        "metadata": self.metadata, "challenge": "b" * 64}
        self.events = []
        self.row = None
        test = self
        class DB:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): test.events.append("lock")
            def connection(self, **kwargs): test.events.append(kwargs)
            def get(self, model, ident): return test.row
            def add(self, value): test.row = value; test.events.append("add")
            def commit(self): test.events.append("commit")
            def rollback(self): test.events.append("rollback")
        self.env = {"UUID": UUID, "datetime": datetime, "timezone": timezone,
            "operation_uuid": operation_uuid, "selected_metadata": selected_metadata,
            "settings": SimpleNamespace(publication_selection_max_bytes=1024 * 1024, media_root=self.target),
            "re": __import__("re"), "SessionLocal": DB, "text": lambda value: value, "TARGET_LOCK": 1,
            "require_destination": lambda db: None,
            "PublicationGeneration": lambda **values: SimpleNamespace(**values),
            "current_head": lambda db: ({"generation_id": self.ident, "version": 1} if self.row and
                self.row.receipt_json.get("outcome") != "not_applied" else {"generation_id": None, "version": 0}),
            "generation_root": generation_root, "build_media_inventory": build_media_inventory,
            "observe_receipt": observe_receipt,
            "observe_database": lambda db, manifest: {name: {} for name in COLLECTIONS},
            "install_generation": lambda *args: self.fail("A new apply must respect the destination allowance."),
        }
        path = Path(__file__).parents[1] / "app/scripts/publication_generation.py"
        wanted = {"_request", "_require_apply_allowance", "_not_applied", "_observe",
                  "apply_generation", "reconcile_generation", "abort_generation"}
        nodes = [n for n in ast.parse(path.read_text()).body if getattr(n, "name", None) in wanted]
        self.assertEqual({n.name for n in nodes}, wanted)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.env)

    def call(self, name):
        args = (self.payload, self.root / "unused-upload") if name == "apply_generation" else (self.payload,)
        return self.env[name](*args)

    def test_source_two_mib_selection_exceeds_one_mib_target_without_starting_import(self):
        self.assertEqual(sum(v["size_bytes"] for v in self.metadata["media_inventory"].values()), 1536 * 1024)
        with self.assertRaisesRegex(ValueError, "destination byte allowance"):
            self.call("apply_generation")
        self.assertIsNone(self.row)
        self.assertNotIn("commit", self.events)
        self.assertFalse((self.target / ".loci-publication-generations").exists())

    def test_oversize_never_applied_operation_can_reconcile_cancel_and_fence_late_delivery(self):
        self.assertIsNone(self.call("reconcile_generation")["receipt"])
        receipt = self.call("abort_generation")["receipt"]
        validate_cancellation_receipt(self.ident, self.payload["selection_digest"], receipt)
        self.assertEqual(receipt["reason"], "explicit_cancel")
        self.assertEqual(self.events.count("commit"), 1)
        self.assertEqual(self.call("apply_generation")["receipt"], receipt)
        self.assertEqual(self.call("reconcile_generation")["receipt"], receipt)
        self.assertEqual(self.call("abort_generation")["receipt"], receipt)
        self.assertEqual(self.events.count("commit"), 1)
        self.assertFalse((self.target / ".loci-publication-generations").exists())

    def test_lowering_policy_after_commit_preserves_verified_history_and_duplicate_resolution(self):
        self.env["settings"].publication_selection_max_bytes = 2 * 1024 * 1024
        self.env["_require_apply_allowance"](self.metadata)
        selected = load_selection(self.source, self.ident, self.ident, self.payload["selection_digest"], self.metadata)
        install_generation(self.target, self.ident, self.payload["selection_digest"], self.metadata, selected["bundle"])
        receipt = {**expected_receipt(self.metadata["manifest"], self.metadata["media_inventory"]),
            "operation_id": self.ident, "generation_id": self.ident,
            "selection_digest": self.payload["selection_digest"], "committed_at": "2026-01-01T00:00:00Z"}
        self.row = SimpleNamespace(generation_id=UUID(self.ident), selection_digest=self.payload["selection_digest"],
                                   receipt_json=receipt)
        self.env["settings"].publication_selection_max_bytes = 1024 * 1024
        for method in ("reconcile_generation", "abort_generation", "apply_generation"):
            with self.subTest(method=method): self.assertEqual(self.call(method)["receipt"], receipt)
        self.assertNotIn("commit", self.events)

    def test_changed_head_fences_oversize_request_without_any_new_apply_policy_or_files(self):
        self.env["current_head"] = lambda db: {"generation_id": None, "version": 1}
        self.env["_require_apply_allowance"] = lambda metadata: self.fail("A no-effect CAS fence needs no byte admission.")
        receipt = self.call("apply_generation")["receipt"]
        validate_cancellation_receipt(self.ident, self.payload["selection_digest"], receipt)
        self.assertEqual(receipt["reason"], "destination_changed")
        self.assertEqual(self.events.count("commit"), 1)
        self.assertFalse((self.target / ".loci-publication-generations").exists())

    def test_all_modes_still_refuse_modified_identity_digest_and_challenge(self):
        for field, value in (("operation_id", "invalid"), ("selection_digest", "c" * 64), ("challenge", "short")):
            original = self.payload[field]; self.payload[field] = value
            try:
                for method in ("apply_generation", "reconcile_generation", "abort_generation"):
                    with self.subTest(field=field, method=method), self.assertRaises((ValueError, SelectionError)): self.call(method)
            finally:
                self.payload[field] = original
        self.assertEqual(self.events, [])


if __name__ == "__main__": unittest.main()
