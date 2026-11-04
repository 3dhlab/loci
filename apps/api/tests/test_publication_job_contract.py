"""Generated request/selection/receipt boundaries; no API, Redis or database."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from app.services.publication_job_contract import (
    InvalidPublicationRequest, canonical, public_status, request_identity,
    validate_cancellation_receipt, validate_receipt, validate_selection, validate_superseded_receipt,
)
from app.services.publication_readback import expected_receipt


class PublicationJobContractTests(unittest.TestCase):
    def setUp(self):
        self.operation=str(uuid4())
        self.metadata={"manifest":{"media_paths":[],"objects":[{"id":"generated","name":"Generated record"}]},
            "media_inventory":{},"bundle_sha256":"a"*64,"bundle_bytes":0,"destination_fingerprint":"b"*64,
            "expected_head":{"generation_id":None,"version":0}}
        self.digest=hashlib.sha256(canonical(self.metadata)).hexdigest()
        self.selection={"version":1,"generation_id":self.operation,"selection_ref":self.operation,
            "selection_digest":self.digest,"metadata":self.metadata}
        self.receipt={**expected_receipt(self.metadata["manifest"],{}),"operation_id":self.operation,
            "generation_id":self.operation,"selection_digest":self.digest,"committed_at":"2026-01-01T00:00:00Z",
            "http_readback":{"status":"verified","resource_count":1,"verified_at":"2026-01-01T00:00:01Z"}}

    def test_request_key_and_body_hashes_are_stable_and_content_bound(self):
        key,body=request_identity("generated-request",{"a":1,"b":2})
        self.assertEqual((key,body),request_identity("generated-request",{"b":2,"a":1}))
        self.assertNotEqual(body,request_identity("generated-request",{"a":2})[1])
        self.assertNotEqual(key,request_identity("other-request",{"a":1,"b":2})[0])
        self.assertEqual(len(key),64)

    def test_raw_request_key_is_not_a_persisted_identity(self):
        key,body=request_identity("generated-sensitive-request",{})
        self.assertNotIn("generated",key+body)

    def test_invalid_key_controls_unicode_and_lengths_rejected(self):
        for key in (None,"","with space","/path","x\n","é","x"*257):
            with self.subTest(key=repr(key)),self.assertRaises(InvalidPublicationRequest):request_identity(key,{})
        request_identity("x"*256,{})

    def test_invalid_nonobject_nonfinite_or_oversized_body_rejected(self):
        for body in ([],None,{"value":float("nan")},{"text":"x"*4097}):
            with self.subTest(body=type(body).__name__),self.assertRaises(InvalidPublicationRequest):request_identity("generated",body)

    def test_exact_captured_real_generation_accepted(self):
        self.assertIs(validate_selection(self.operation,self.selection),self.selection)

    def test_metadata_digest_cannot_be_rebound(self):
        self.metadata["manifest"]["objects"][0]["name"]="Changed after selection"
        with self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,self.selection)

    def test_selection_ref_or_generation_cannot_name_another_operation_or_path(self):
        for name,value in (("selection_ref","../private"),("selection_ref",str(uuid4())),("generation_id",str(uuid4()))):
            selected=deepcopy(self.selection);selected[name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,selected)

    def test_selection_unknown_fields_and_boolean_version_rejected(self):
        for selected in ({**self.selection,"version":True},{**self.selection,"untrusted":True}):
            with self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,selected)

    def test_metadata_shape_and_bundle_identity_are_required(self):
        for name,value in (("bundle_bytes",True),("bundle_bytes",-1),("bundle_sha256","A"*64),
                           ("destination_fingerprint","missing"),("manifest",None),("media_inventory",None)):
            selected=deepcopy(self.selection);selected["metadata"][name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,selected)

    def test_extra_metadata_and_inconsistent_media_selection_rejected(self):
        selected=deepcopy(self.selection);selected["metadata"]["unbound_path"]="/generated"
        with self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,selected)
        selected=deepcopy(self.selection);selected["metadata"]["manifest"]["media_paths"]=["missing"]
        selected["selection_digest"]=hashlib.sha256(canonical(selected["metadata"])).hexdigest()
        with self.assertRaises(RuntimeError):validate_selection(self.operation,selected)

    def test_expected_head_is_a_typed_monotonic_capture_identity(self):
        for head in ({"generation_id":None,"version":True},{"generation_id":None,"version":-1},
                     {"generation_id":"../private","version":0},{"generation_id":None,"version":0,"extra":True}):
            selected=deepcopy(self.selection);selected["metadata"]["expected_head"]=head
            selected["selection_digest"]=hashlib.sha256(canonical(selected["metadata"])).hexdigest()
            with self.assertRaises(InvalidPublicationRequest):validate_selection(self.operation,selected)

    def test_complete_destination_database_media_and_http_receipt_accepted(self):
        self.assertIs(validate_receipt(self.operation,self.digest,self.receipt,self.metadata),self.receipt)

    def test_receipt_other_operation_generation_selection_or_boolean_version_rejected(self):
        for name,value in (("operation_id",str(uuid4())),("generation_id",str(uuid4())),
                           ("selection_digest","c"*64),("version",True)):
            receipt=deepcopy(self.receipt);receipt[name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_receipt_requires_http_readback_and_cannot_trust_database_commit_alone(self):
        for value in (None,{"status":"pending"},{"status":"verified","resource_count":True,"verified_at":"2026-01-01T00:00:00Z"}):
            receipt=deepcopy(self.receipt);receipt["http_readback"]=value
            with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_unallowlisted_receipt_or_http_fields_cannot_expose_private_target_data(self):
        receipt=deepcopy(self.receipt);receipt["private_diagnostics"]="GENERATED_SECRET"
        with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_http_observation_digest_is_optional_but_typed_and_content_shaped(self):
        receipt=deepcopy(self.receipt);receipt["http_readback"]["observations_sha256"]="e"*64
        validate_receipt(self.operation,self.digest,receipt,self.metadata)
        for value in (None,"bad","A"*64):
            receipt["http_readback"]["observations_sha256"]=value
            with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)
        receipt=deepcopy(self.receipt);receipt["http_readback"]["private_diagnostics"]="GENERATED_SECRET"
        with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_receipt_requires_all_hashes_and_typed_nonnegative_counts(self):
        for name,value in (("media_sha256","BAD"),("records_sha256",None),("selection_sha256","A"*64),
                           ("row_counts",{"objects":True}),("row_counts",{"objects":-1}),("media_file_count",False)):
            receipt=deepcopy(self.receipt);receipt[name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_receipt_cannot_substitute_other_records_or_media_with_same_operation_id(self):
        for name,value in (("records_sha256","c"*64),("media_sha256","d"*64),
                           ("selection_sha256","e"*64),("row_counts",{"objects":100}),("media_file_count",10)):
            receipt=deepcopy(self.receipt);receipt[name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_receipt_timestamps_must_be_timezone_aware(self):
        for timestamp in (None,"invalid","2026-01-01T00:00:00"):
            receipt=deepcopy(self.receipt);receipt["committed_at"]=timestamp
            with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)
        receipt=deepcopy(self.receipt);receipt["http_readback"]["verified_at"]="invalid"
        with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def cancellation(self):
        return {"version":1,"outcome":"not_applied","operation_id":self.operation,"generation_id":self.operation,
            "selection_digest":self.digest,"reason":"explicit_cancel","committed_at":"2026-01-01T00:00:00Z"}

    def superseded(self):
        return {"version":1,"outcome":"superseded","operation_id":self.operation,"generation_id":self.operation,
            "selection_digest":self.digest,"committed_at":self.receipt["committed_at"],
            "current_head":{"generation_id":str(uuid4()),"version":1},
            "published_receipt":{name:value for name,value in self.receipt.items() if name!="http_readback"}}

    def test_negative_fence_is_separate_from_verified_publication(self):
        receipt=self.cancellation();validate_cancellation_receipt(self.operation,self.digest,receipt)
        with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)
        for name,value in (("outcome","published"),("operation_id",str(uuid4())),("reason","unsupported"),
                           ("version",True),("committed_at","2026-01-01T00:00:00")):
            changed=deepcopy(receipt);changed[name]=value
            with self.subTest(name=name),self.assertRaises(InvalidPublicationRequest):validate_cancellation_receipt(self.operation,self.digest,changed)

    def test_superseded_resolution_retains_committed_history_without_current_http_claim(self):
        receipt=self.superseded();validate_superseded_receipt(self.operation,self.digest,receipt,self.metadata)
        self.assertNotIn("http_readback",receipt["published_receipt"])
        with self.assertRaises(InvalidPublicationRequest):validate_receipt(self.operation,self.digest,receipt,self.metadata)

    def test_superseded_resolution_rejects_same_or_older_head_and_changed_history(self):
        for head in ({"generation_id":self.operation,"version":1},{"generation_id":str(uuid4()),"version":0},
                     {"generation_id":None,"version":True}):
            receipt=self.superseded();receipt["current_head"]=head
            with self.assertRaises(InvalidPublicationRequest):validate_superseded_receipt(self.operation,self.digest,receipt,self.metadata)
        for key,value in (("media_sha256","d"*64),("row_counts",{"objects":True}),("operation_id",str(uuid4()))):
            receipt=self.superseded();receipt["published_receipt"][key]=value
            with self.assertRaises(InvalidPublicationRequest):validate_superseded_receipt(self.operation,self.digest,receipt,self.metadata)

    def job(self,state):
        return SimpleNamespace(id=self.operation,status=state,destination="configured-public",selection_digest=self.digest,
            selection_metadata_json=self.metadata,stage_key="generated",detail="Generated status.",progress_percent=20,
            started_at=datetime(2026,1,1,tzinfo=timezone.utc),finished_at=None,error_code=None,receipt_json=self.receipt)

    def test_public_status_never_returns_selection_content_paths_or_raw_request_key(self):
        status=public_status(self.job("QUEUED"));encoded=json.dumps(status)
        for value in ("Generated record","selection_ref","selection_metadata_json","bundle_sha256","request_key"):
            self.assertNotIn(value,encoded)
        self.assertEqual(status["object_count"],1)
        self.assertIsNone(status["receipt"])

    def test_only_succeeded_status_exposes_verified_receipt(self):
        for state in ("QUEUED","RUNNING","NEEDS_RECONCILIATION","FAILED","CANCELLED","SUPERSEDED"):
            with self.subTest(state=state):self.assertIsNone(public_status(self.job(state))["receipt"])
        self.assertEqual(public_status(self.job("SUCCEEDED"))["receipt"],self.receipt)

    def test_cancelled_and_superseded_receipts_never_appear_as_current_verified_output(self):
        job=self.job("CANCELLED");job.receipt_json=self.cancellation()
        status=public_status(job);self.assertIsNone(status["receipt"]);self.assertEqual(status["cancellation_receipt"],job.receipt_json)
        job=self.job("SUPERSEDED");job.receipt_json=self.superseded()
        status=public_status(job);self.assertIsNone(status["receipt"]);self.assertEqual(status["superseded_receipt"],job.receipt_json)

    def test_recovery_status_carries_exact_generation_and_durable_lookup_capability(self):
        status=public_status(self.job("NEEDS_RECONCILIATION"))
        self.assertEqual(status["operation_id"],self.operation)
        self.assertEqual(status["generation_id"],self.operation)
        self.assertEqual(status["selection_digest"],self.digest)
        self.assertTrue(status["publication_capabilities"]["request_lookup"])
        self.assertTrue(status["publication_capabilities"]["reconciliation"])


if __name__=="__main__":unittest.main()
