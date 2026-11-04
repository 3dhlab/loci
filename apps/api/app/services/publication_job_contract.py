"""Pure validation and public state for durable real publication jobs."""
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from app.services.publication_readback import expected_receipt

DESTINATION = "configured-public"
ACTIVE = {"QUEUED", "RUNNING", "NEEDS_RECONCILIATION"}
CAPABILITIES = {"durable_requests": True, "request_lookup": True, "reconciliation": True, "cancellation": True}
HEX = re.compile(r"[0-9a-f]{64}")
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


class InvalidPublicationRequest(ValueError): pass
class PublicationRequestConflict(ValueError): pass
class PublicationLaneBusy(ValueError): pass
class PublicationNotFound(LookupError): pass
class PublicationAuthorityRevoked(PermissionError): pass
class PublicationAdminRequired(PermissionError): pass
class StalePublicationAttempt(RuntimeError): pass


def canonical(value):
    try:
        return json.dumps(value,sort_keys=True,separators=(",", ":"),ensure_ascii=True,allow_nan=False).encode()
    except (TypeError,ValueError,UnicodeError):
        raise InvalidPublicationRequest("Invalid publication data.") from None


def request_identity(request_key, body):
    if not isinstance(request_key,str) or KEY.fullmatch(request_key) is None:
        raise InvalidPublicationRequest("A valid Idempotency-Key is required.")
    if not isinstance(body,dict):
        raise InvalidPublicationRequest("Publication body must be an object.")
    encoded=canonical(body)
    if len(encoded)>4096:
        raise InvalidPublicationRequest("Publication request is too large.")
    return hashlib.sha256(request_key.encode()).hexdigest(),hashlib.sha256(encoded).hexdigest()


def validate_selection(operation_id, selection):
    operation_id=str(UUID(str(operation_id)))
    if (not isinstance(selection,dict) or set(selection)!={"version","generation_id","selection_ref","selection_digest","metadata"}
            or type(selection.get("version")) is not int or selection["version"]!=1
            or selection.get("generation_id")!=operation_id or selection.get("selection_ref")!=operation_id
            or not isinstance(selection.get("selection_digest"),str)
            or HEX.fullmatch(selection["selection_digest"]) is None
            or not isinstance(selection.get("metadata"),dict)):
        raise InvalidPublicationRequest("Selected publication generation is invalid.")
    metadata=selection["metadata"]
    if (set(metadata)!={"manifest","media_inventory","bundle_sha256","bundle_bytes","destination_fingerprint","expected_head"}
            or not isinstance(metadata.get("manifest"),dict) or not isinstance(metadata.get("media_inventory"),dict)
            or not isinstance(metadata.get("bundle_sha256"),str) or HEX.fullmatch(metadata["bundle_sha256"]) is None
            or type(metadata.get("bundle_bytes")) is not int or metadata["bundle_bytes"]<0
            or not isinstance(metadata.get("destination_fingerprint"),str)
            or HEX.fullmatch(metadata["destination_fingerprint"]) is None
            or len(canonical(metadata))>32*1024*1024):
        raise InvalidPublicationRequest("Selected publication metadata is invalid.")
    head=metadata["expected_head"]
    if (not isinstance(head,dict) or set(head)!={"generation_id","version"}
            or type(head["version"]) is not int or not 0<=head["version"]<=2**63-1):
        raise InvalidPublicationRequest("Selected destination head is invalid.")
    if head["generation_id"] is not None:
        try:
            if not isinstance(head["generation_id"],str) or str(UUID(head["generation_id"]))!=head["generation_id"]:
                raise ValueError()
        except ValueError:
            raise InvalidPublicationRequest("Selected destination head identity is invalid.") from None
    if hashlib.sha256(canonical(metadata)).hexdigest()!=selection["selection_digest"]:
        raise InvalidPublicationRequest("Selected publication digest does not match its metadata.")
    expected_receipt(metadata["manifest"],metadata["media_inventory"])
    return selection


def validate_receipt(operation_id, selection_digest, receipt, metadata=None):
    operation_id=str(UUID(str(operation_id)))
    fields={"version","operation_id","generation_id","selection_digest","records_sha256","media_sha256",
            "selection_sha256","row_counts","media_file_count","committed_at","http_readback"}
    if (not isinstance(receipt,dict) or set(receipt)!=fields
            or type(receipt.get("version")) is not int or receipt["version"]!=1
            or receipt.get("operation_id")!=operation_id or receipt.get("generation_id")!=operation_id
            or receipt.get("selection_digest")!=selection_digest):
        raise InvalidPublicationRequest("Destination receipt identity does not match.")
    for name in ("selection_digest","records_sha256","media_sha256","selection_sha256"):
        if not isinstance(receipt.get(name),str) or HEX.fullmatch(receipt[name]) is None:
            raise InvalidPublicationRequest("Destination receipt digest is invalid.")
    counts=receipt.get("row_counts")
    http=receipt.get("http_readback")
    if (not isinstance(counts,dict) or any(not isinstance(k,str) or type(v) is not int or v<0 for k,v in counts.items())
            or type(receipt.get("media_file_count")) is not int or receipt["media_file_count"]<0
            or not isinstance(http,dict) or not {"status","resource_count","verified_at"}<=set(http)
            or not set(http)<={"status","resource_count","verified_at","observations_sha256"} or http.get("status")!="verified"
            or type(http.get("resource_count")) is not int or not 0<=http["resource_count"]<=2**53-1):
        raise InvalidPublicationRequest("Destination record/media/HTTP readback is incomplete.")
    if ("observations_sha256" in http and (not isinstance(http["observations_sha256"],str)
            or HEX.fullmatch(http["observations_sha256"]) is None)):
        raise InvalidPublicationRequest("Destination HTTP observation digest is invalid.")
    for value in (receipt.get("committed_at"),http.get("verified_at")):
        try:
            if not isinstance(value,str) or datetime.fromisoformat(value.replace("Z","+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise InvalidPublicationRequest("Destination receipt timestamp is invalid.") from None
    if len(canonical(receipt))>65536:
        raise InvalidPublicationRequest("Destination receipt is too large.")
    if metadata is not None:
        expected=expected_receipt(metadata["manifest"],metadata["media_inventory"])
        if any(receipt.get(name)!=expected[name] for name in (
                "records_sha256","media_sha256","selection_sha256","row_counts","media_file_count")):
            raise InvalidPublicationRequest("Destination receipt does not match the selected records/media.")
    return receipt


def validate_cancellation_receipt(operation_id,selection_digest,receipt):
    fields={"version","outcome","operation_id","generation_id","selection_digest","reason","committed_at"}
    operation_id=str(UUID(str(operation_id)))
    if (not isinstance(receipt,dict) or set(receipt)!=fields
            or type(receipt.get("version")) is not int or receipt["version"]!=1
            or receipt.get("outcome")!="not_applied" or receipt.get("reason") not in {"destination_changed","explicit_cancel"}
            or receipt.get("operation_id")!=operation_id or receipt.get("generation_id")!=operation_id
            or receipt.get("selection_digest")!=selection_digest
            or not isinstance(selection_digest,str) or HEX.fullmatch(selection_digest) is None):
        raise InvalidPublicationRequest("Destination cancellation fence does not match this selection.")
    try:
        value=receipt["committed_at"]
        if not isinstance(value,str) or datetime.fromisoformat(value.replace("Z","+00:00")).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise InvalidPublicationRequest("Destination cancellation fence timestamp is invalid.") from None
    return receipt


def validate_superseded_receipt(operation_id,selection_digest,receipt,metadata):
    operation_id=str(UUID(str(operation_id)))
    fields={"version","outcome","operation_id","generation_id","selection_digest","committed_at","current_head","published_receipt"}
    if (not isinstance(receipt,dict) or set(receipt)!=fields or type(receipt.get("version")) is not int
            or receipt["version"]!=1 or receipt.get("outcome")!="superseded"
            or receipt.get("operation_id")!=operation_id or receipt.get("generation_id")!=operation_id
            or receipt.get("selection_digest")!=selection_digest):
        raise InvalidPublicationRequest("Superseded destination resolution identity is invalid.")
    head=receipt["current_head"]
    if (not isinstance(head,dict) or set(head)!={"generation_id","version"}
            or type(head["version"]) is not int or not metadata["expected_head"]["version"]<head["version"]<=2**63-1
            or head["generation_id"]==operation_id):
        raise InvalidPublicationRequest("Superseded destination head is invalid.")
    if head["generation_id"] is not None:
        try:
            if not isinstance(head["generation_id"],str) or str(UUID(head["generation_id"]))!=head["generation_id"]:
                raise ValueError()
        except ValueError:
            raise InvalidPublicationRequest("Superseded destination head identity is invalid.") from None
    base=receipt["published_receipt"]
    expected=expected_receipt(metadata["manifest"],metadata["media_inventory"])
    if (not isinstance(base,dict) or set(base)!=set(expected)|{"operation_id","generation_id","selection_digest","committed_at"}
            or canonical({name:base.get(name) for name in expected})!=canonical(expected)
            or base.get("operation_id")!=operation_id or base.get("generation_id")!=operation_id
            or base.get("selection_digest")!=selection_digest or base.get("committed_at")!=receipt["committed_at"]):
        raise InvalidPublicationRequest("Superseded history does not match the original committed selection.")
    try:
        value=receipt["committed_at"]
        if not isinstance(value,str) or datetime.fromisoformat(value.replace("Z","+00:00")).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise InvalidPublicationRequest("Superseded commitment timestamp is invalid.") from None
    if len(canonical(receipt))>65536:raise InvalidPublicationRequest("Superseded receipt is too large.")
    return receipt


def public_status(job):
    manifest=job.selection_metadata_json.get("manifest",{})
    def instant(value):return value.isoformat() if value is not None else None
    counts={name:len(manifest.get(collection,[])) for name,collection in (
        ("project_count","projects"),("object_count","objects"),("video_count","videos"),
        ("transcript_count","transcripts"),("annotation_count","object_model_annotations"))}
    return {"operation_id":str(job.id),"state":job.status,"destination":job.destination,
        "publication_capabilities":dict(CAPABILITIES),"selection_digest":job.selection_digest,
        "generation_id":str(job.id),"stage_key":job.stage_key,"stage_label":job.status.replace("_"," ").title(),
        "detail":job.detail,"progress_percent":job.progress_percent,
        "started_at":instant(job.started_at),"finished_at":instant(job.finished_at),
        "generated_at":manifest.get("generated_at"),"media_file_count":len(job.selection_metadata_json.get("media_inventory",{})),
        "error":job.error_code,"receipt":job.receipt_json if job.status=="SUCCEEDED" else None,
        "cancellation_receipt":job.receipt_json if job.status=="CANCELLED" else None,
        "superseded_receipt":job.receipt_json if job.status=="SUPERSEDED" else None,**counts}
