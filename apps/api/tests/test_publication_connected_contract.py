"""Connect real sealed bytes, target observations and source receipt validators.

These small generated-file tests execute unchanged capture/target/transport
function bodies. SQL rows, SSH replies and anonymous HTTP responses are injected
boundaries. No database, Redis, SSH, server or browser is started; those remain
independent acceptance requirements.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import io
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import UUID, uuid4

from app.services.publication_generation_files import (
    generation_root, install_generation, selected_metadata,
)
from app.services.publication_http_readback import verify_http
from app.services.publication_job_contract import (
    InvalidPublicationRequest, validate_cancellation_receipt, validate_receipt,
    validate_selection, validate_superseded_receipt,
)
from app.services.publication_media_inventory import build_media_inventory
from app.services.publication_readback import (
    COLLECTIONS, PublicationReadbackError, canonical, expected_receipt, observe_receipt,
)
from app.services.publication_selection_store import (
    SelectionError, load_selection, operation_uuid, seal_selection,
)


def actual_body(relative_path, name, boundaries):
    """Compile the named real body unchanged, replacing imported boundaries only."""
    path = Path(__file__).parents[1] / relative_path
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(nodes) != 1:
        raise AssertionError(f"Expected one real function {name}")
    namespace = dict(boundaries)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class ConnectedPublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-connected-generated-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.source, self.target = root / "source", root / "target"
        self.source.mkdir(); self.target.mkdir()
        (self.source / "models").mkdir()
        self.media = b"generated contract model bytes"
        (self.source / "models/generated.glb").write_bytes(self.media)
        self.operation, self.object_id, self.model_id = (str(uuid4()) for _ in range(3))
        self.manifest = {
            "media_paths": ["models/generated.glb"],
            "objects": [{"id": self.object_id, "name": "Generated selected object", "is_published": True}],
            "object_models": [{"id": self.model_id, "object_id": self.object_id,
                               "public_media_relpath": "models/generated.glb",
                               "sha256_checksum": hashlib.sha256(self.media).hexdigest(),
                               "file_size_bytes": len(self.media)}],
        }
        self.head = {"generation_id": str(uuid4()), "version": 7}
        self.fingerprint = "a" * 64
        self.events = []
        self.capture = actual_body("app/services/publication_transport.py", "capture_selection", {
            "operation_uuid": operation_uuid,
            "_configuration": lambda: ("generated-config", "https://public.example.invalid", self.fingerprint),
            "_roundtrip": self.capture_roundtrip,
            "build_projection": lambda: (deepcopy(self.manifest), list(self.manifest["media_paths"])),
            "seal_selection": seal_selection,
            "settings": SimpleNamespace(media_root=self.source, publication_selection_max_bytes=4096),
        })
        self.selected = self.capture(self.operation, {"type": "user", "user_id": str(uuid4())})
        self.metadata = self.selected["metadata"]
        self.selection_digest = self.selected["selection_digest"]
        self.payload = {"operation_id": self.operation, "selection_digest": self.selection_digest,
                        "metadata": self.metadata, "challenge": "b" * 64}

    def capture_roundtrip(self, config, mode):
        self.events.append(mode)
        return {"head": deepcopy(self.head)} if mode == "head" else {"protocol": "loci-durable-publication", "version": 1}

    def installed(self):
        selected = load_selection(self.source, self.operation, self.selected["selection_ref"],
                                  self.selection_digest, self.metadata)
        return install_generation(self.target, self.operation, self.selection_digest,
                                  self.metadata, selected["bundle"])

    def committed_row(self):
        return SimpleNamespace(generation_id=UUID(self.operation), selection_digest=self.selection_digest,
            receipt_json={**expected_receipt(self.manifest, self.metadata["media_inventory"]),
                          "operation_id": self.operation, "generation_id": self.operation,
                          "selection_digest": self.selection_digest,
                          "committed_at": "2026-01-01T00:00:00+00:00"})

    def observed_rows(self):
        rows = {name: {} for name in COLLECTIONS}
        rows["objects"][self.object_id] = deepcopy(self.manifest["objects"][0])
        model = deepcopy(self.manifest["object_models"][0])
        model["storage_path"] = str(generation_root(self.target, self.operation) / model.pop("public_media_relpath"))
        rows["object_models"][self.model_id] = model
        return rows

    def target_observer(self, head):
        return actual_body("app/scripts/publication_generation.py", "_observe", {
            "current_head": lambda db: deepcopy(head),
            "generation_root": generation_root,
            "settings": SimpleNamespace(media_root=self.target),
            "observe_database": lambda db, manifest: self.observed_rows(),
            "build_media_inventory": build_media_inventory,
            "observe_receipt": observe_receipt,
        })

    def http_opener(self):
        test = self
        class Response(io.BytesIO):
            status = 200
            def __init__(self, url, body): super().__init__(body); self.url = url
            def geturl(self): return self.url
        class Opener:
            def open(self, request, timeout):
                test.assertNotIn("Authorization", request.headers)
                test.assertGreater(timeout, 0)
                path = request.full_url.removeprefix("https://public.example.invalid")
                if path == "/api/v1/public/objects": value = [{"id": test.object_id}]
                elif path == f"/api/v1/public/objects/{test.object_id}/preview":
                    value = {"object": test.manifest["objects"][0], "annotations": []}
                elif path == f"/api/v1/public/objects/{test.object_id}/model/file":
                    return Response(request.full_url, test.media)
                else: raise AssertionError(f"Unexpected route: {path}")
                return Response(request.full_url, json.dumps(value).encode())
        return Opener()

    def test_real_capture_seal_is_accepted_by_source_and_target_validators(self):
        self.assertIs(validate_selection(self.operation, self.selected), self.selected)
        selected_metadata(self.selection_digest, self.metadata)
        self.assertEqual(self.metadata["expected_head"], self.head)
        self.assertEqual(self.events, ["protocol", "head"])
        changed = deepcopy(self.selected)
        changed["metadata"]["expected_head"]["version"] += 1
        with self.assertRaises(InvalidPublicationRequest): validate_selection(self.operation, changed)

    def test_generated_sealed_bytes_survive_source_edit_without_reselection(self):
        (self.source / "models/generated.glb").write_bytes(b"later authoring edit")
        self.assertEqual((self.installed() / "models/generated.glb").read_bytes(), self.media)
        validate_selection(self.operation, self.selected)

    def test_actual_target_negative_receipts_satisfy_only_cancellation_contract(self):
        for reason in ("destination_changed", "explicit_cancel"):
            committed = []
            db = SimpleNamespace(add=committed.append, commit=lambda: committed.append("commit"))
            negative = actual_body("app/scripts/publication_generation.py", "_not_applied", {
                "datetime": datetime, "timezone": timezone, "UUID": UUID,
                "PublicationGeneration": lambda **values: SimpleNamespace(**values),
            })(db, self.payload, reason)
            self.assertEqual(negative["challenge"], self.payload["challenge"])
            self.assertEqual(committed[-1], "commit")
            self.assertEqual(committed[0].receipt_json, negative["receipt"])
            validate_cancellation_receipt(self.operation, self.selection_digest, negative["receipt"])
            with self.assertRaises(InvalidPublicationRequest):
                validate_receipt(self.operation, self.selection_digest, negative["receipt"], self.metadata)

    def test_actual_target_superseded_receipt_retains_content_bound_history(self):
        head = {"generation_id": str(uuid4()), "version": 9}
        receipt = self.target_observer(head)(None, self.payload, self.committed_row())
        validate_superseded_receipt(self.operation, self.selection_digest, receipt, self.metadata)
        self.assertNotIn("http_readback", receipt["published_receipt"])
        changed = deepcopy(receipt); changed["published_receipt"]["media_sha256"] = "f" * 64
        with self.assertRaises(InvalidPublicationRequest):
            validate_superseded_receipt(self.operation, self.selection_digest, changed, self.metadata)

    def test_actual_target_files_and_transport_http_result_satisfy_source_success_contract(self):
        self.installed()
        observer = self.target_observer({"generation_id": self.operation, "version": 8})
        calls = []
        def roundtrip(config, mode, payload):
            calls.append(mode)
            return {"receipt": observer(None, payload, self.committed_row()), "challenge": payload["challenge"]}
        confirm = actual_body("app/services/publication_transport.py", "_confirm", {
            "_roundtrip": roundtrip, "expected_receipt": expected_receipt, "canonical": canonical,
            "verify_http": lambda manifest, inventory, base: verify_http(manifest, inventory, base, opener=self.http_opener()),
            "secrets": SimpleNamespace(token_hex=lambda count: "c" * (count * 2)), "SelectionError": SelectionError,
        })
        receipt = confirm("generated-config", "https://public.example.invalid", self.payload)
        validate_receipt(self.operation, self.selection_digest, receipt, self.metadata)
        self.assertEqual(receipt["http_readback"]["resource_count"], 3)
        self.assertRegex(receipt["http_readback"]["observations_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(calls, ["reconcile", "reconcile"])

    def test_installed_media_corruption_prevents_target_receipt_observation(self):
        root = self.installed(); media = root / "models/generated.glb"
        os.chmod(media, 0o600); media.write_bytes(b"corrupt generation")
        observer = self.target_observer({"generation_id": self.operation, "version": 8})
        with self.assertRaises(PublicationReadbackError) as error: observer(None, self.payload, self.committed_row())
        self.assertIn("readback", str(error.exception).lower())

    def test_target_switch_during_http_never_becomes_source_success(self):
        self.installed()
        current = self.target_observer({"generation_id": self.operation, "version": 8})
        newer = self.target_observer({"generation_id": str(uuid4()), "version": 9})
        observers = iter((current, newer))
        confirm = actual_body("app/services/publication_transport.py", "_confirm", {
            "_roundtrip": lambda config, mode, payload: {"receipt": next(observers)(None, payload, self.committed_row())},
            "expected_receipt": expected_receipt, "canonical": canonical,
            "verify_http": lambda manifest, inventory, base: verify_http(manifest, inventory, base, opener=self.http_opener()),
            "secrets": SimpleNamespace(token_hex=lambda count: "d" * (count * 2)), "SelectionError": SelectionError,
        })
        with self.assertRaisesRegex(SelectionError, "changed during HTTP"):
            confirm("generated-config", "https://public.example.invalid", self.payload)

    def test_target_negative_fence_passes_through_transport_without_http_claim(self):
        negative = actual_body("app/scripts/publication_generation.py", "_not_applied", {
            "datetime": datetime, "timezone": timezone, "UUID": UUID,
            "PublicationGeneration": lambda **values: SimpleNamespace(**values),
        })(SimpleNamespace(add=lambda row: None, commit=lambda: None), self.payload, "explicit_cancel")["receipt"]
        confirm = actual_body("app/services/publication_transport.py", "_confirm", {
            "_roundtrip": lambda *args: {"receipt": negative},
            "verify_http": lambda *args: self.fail("A negative fence cannot claim current HTTP publication."),
        })
        self.assertIs(confirm("generated-config", "https://public.example.invalid", self.payload), negative)
        validate_cancellation_receipt(self.operation, self.selection_digest, negative)


if __name__ == "__main__":
    unittest.main()
