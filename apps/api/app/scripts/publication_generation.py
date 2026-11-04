"""Idempotent destination generation commit and read-only reconciliation.

Uploaded files are installed under a fresh immutable prefix. All projected row
references, the immutable generation receipt and current head commit together.
Older generation files are retained for rollback; this command never prunes.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from uuid import UUID

from sqlalchemy import select, text

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.publication_job import PublicationDestinationHead, PublicationGeneration
from app.models.entities import Project
from app.scripts.import_public_projection import PUBLIC_OWNER_ID, import_projection
from app.scripts.verify_public_projection import observe_database
from app.services.publication_generation_files import (
    generation_manifest, generation_root, install_generation, selected_metadata,
)
from app.services.publication_media_inventory import build_media_inventory
from app.services.publication_readback import canonical, observe_receipt
from app.services.publication_selection_store import MAX_METADATA, operation_uuid

TARGET_LOCK = 0x4c4f43495055424c


def require_destination(db):
    if not settings.publication_destination_enabled:
        raise ValueError("A dedicated public-projection destination must be explicitly enabled.")
    if db.scalar(select(Project.id).where(Project.owner_id != PUBLIC_OWNER_ID).limit(1)) is not None:
        raise ValueError("An authoring database cannot be used as a publication destination.")


def current_head(db):
    head = db.get(PublicationDestinationHead, "global")
    return {"generation_id": str(head.generation_id) if head and head.generation_id else None,
            "version": head.version if head else 0}


def _not_applied(db, payload, reason):
    committed = datetime.now(timezone.utc)
    receipt = {"version": 1, "outcome": "not_applied", "reason": reason,
               "operation_id": payload["operation_id"], "generation_id": payload["operation_id"],
               "selection_digest": payload["selection_digest"], "committed_at": committed.isoformat()}
    ident = UUID(payload["operation_id"])
    db.add(PublicationGeneration(operation_id=ident, generation_id=ident,
                                selection_digest=payload["selection_digest"],
                                receipt_json=receipt, committed_at=committed))
    db.commit()
    return {"receipt": receipt, "challenge": payload["challenge"]}


def _request(payload):
    if (not isinstance(payload, dict) or set(payload) != {
            "operation_id", "selection_digest", "metadata", "challenge"}
            or not isinstance(payload["challenge"], str)
            or re.fullmatch("[0-9a-f]{64}", payload["challenge"]) is None):
        raise ValueError("Invalid generation request.")
    operation_uuid(payload["operation_id"])
    selected_metadata(payload["selection_digest"], payload["metadata"])
    return UUID(payload["operation_id"])


def _require_apply_allowance(metadata):
    """Admission policy for a new effect, never a barrier to resolving history."""
    limit = settings.publication_selection_max_bytes
    if (sum(row["size_bytes"] for row in metadata["media_inventory"].values()) > limit
            or metadata["bundle_bytes"] > limit + limit // 100 + 1024 * 1024):
        raise ValueError("Selected generation exceeds the destination byte allowance.")


def _observe(db, payload, row):
    if row.selection_digest != payload["selection_digest"]:
        raise ValueError("Generation identity conflicts with a committed operation.")
    if row.receipt_json.get("outcome") == "not_applied":
        return row.receipt_json
    head = current_head(db)
    if head["generation_id"] != str(row.generation_id):
        return {"version": 1, "outcome": "superseded", "operation_id": payload["operation_id"],
                "generation_id": payload["operation_id"], "selection_digest": payload["selection_digest"],
                "committed_at": row.receipt_json["committed_at"], "current_head": head,
                "published_receipt": row.receipt_json}
    metadata = payload["metadata"]
    root = generation_root(settings.media_root, payload["operation_id"])
    records = observe_database(db, metadata["manifest"])
    inventory = build_media_inventory(root, sorted(metadata["media_inventory"]))
    observed = observe_receipt(metadata["manifest"], metadata["media_inventory"],
                               records, inventory, media_root=root)
    if any(row.receipt_json.get(k) != observed[k] for k in observed):
        raise ValueError("Committed generation receipt differs from readback.")
    return row.receipt_json


def apply_generation(payload, bundle):
    ident = _request(payload)
    metadata = payload["metadata"]
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": TARGET_LOCK})
        require_destination(db)
        row = db.get(PublicationGeneration, ident)
        if row is not None:
            # Duplicate delivery performs readback, never another import.
            receipt = _observe(db, payload, row)
            db.rollback()
            return {"receipt": receipt, "challenge": payload["challenge"]}
        if current_head(db) != metadata["expected_head"]:
            # The tombstone commits under the same lock as all supported imports.
            # A late delivery of this operation can never replace a newer head.
            return _not_applied(db, payload, "destination_changed")
        _require_apply_allowance(metadata)
        install_generation(settings.media_root, payload["operation_id"],
                           payload["selection_digest"], metadata, bundle)
        playback = {row["id"]: metadata["media_inventory"][row["public_media_relpath"]]
                    for row in metadata["manifest"].get("videos", [])}
        import_projection(generation_manifest(metadata["manifest"], payload["operation_id"]), db,
                          generation_id=payload["operation_id"], playback_inventory=playback)
        db.flush()
        observed = observe_database(db, metadata["manifest"])
        root = generation_root(settings.media_root, payload["operation_id"])
        inventory = build_media_inventory(root, sorted(metadata["media_inventory"]))
        base = observe_receipt(metadata["manifest"], metadata["media_inventory"],
                               observed, inventory, media_root=root)
        committed_at = datetime.now(timezone.utc)
        receipt = {**base, "operation_id": payload["operation_id"],
                   "generation_id": payload["operation_id"],
                   "selection_digest": payload["selection_digest"],
                   "committed_at": committed_at.isoformat()}
        row = PublicationGeneration(operation_id=ident, generation_id=ident,
                                    selection_digest=payload["selection_digest"],
                                    receipt_json=receipt, committed_at=committed_at)
        db.add(row)
        db.flush()
        head = db.get(PublicationDestinationHead, "global")
        if head is None:
            head = PublicationDestinationHead(lane="global", generation_id=ident,
                                               version=1, updated_at=committed_at)
        else:
            head.generation_id = ident
            head.version += 1
            head.updated_at = committed_at
        db.add(head)
        db.commit()
    # Read committed state in a new transaction, independently of the import.
    return reconcile_generation(payload)


def reconcile_generation(payload):
    ident = _request(payload)
    with SessionLocal() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
        require_destination(db)
        row = db.get(PublicationGeneration, ident)
        receipt = None if row is None else _observe(db, payload, row)
    return {"receipt": receipt, "challenge": payload["challenge"]}


def abort_generation(payload):
    """Fence a never-committed operation without adopting/deleting orphan files.

    If an apply is in flight, its transaction lock resolves first. A committed
    publication is observed, never undone. A tombstone prevents a late apply.
    """
    ident = _request(payload)
    with SessionLocal() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": TARGET_LOCK})
        require_destination(db)
        row = db.get(PublicationGeneration, ident)
        if row is not None:
            return {"receipt": _observe(db, payload, row), "challenge": payload["challenge"]}
        return _not_applied(db, payload, "explicit_cancel")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("protocol", "head", "apply", "reconcile", "abort"))
    parser.add_argument("--bundle")
    args = parser.parse_args()
    if args.mode in {"protocol", "head"}:
        # Fail compatibility before upload if the destination migration is absent.
        with SessionLocal() as db:
            require_destination(db)
            db.execute(text("SELECT operation_id FROM publication_generations LIMIT 0"))
            db.execute(text("SELECT lane FROM publication_destination_head LIMIT 0"))
            result = ({"head": current_head(db)} if args.mode == "head" else
                      {"protocol": "loci-durable-publication", "version": 1})
    else:
        data = sys.stdin.buffer.read(MAX_METADATA + 4097)
        if len(data) > MAX_METADATA + 4096:
            raise ValueError("Generation request is too large.")
        payload = json.loads(data)
        if args.mode == "apply":
            if not args.bundle:
                raise ValueError("A verified generation bundle is required.")
            result = apply_generation(payload, Path(args.bundle))
        elif args.mode == "reconcile":
            result = reconcile_generation(payload)
        else:
            result = abort_generation(payload)
    sys.stdout.buffer.write(canonical(result) + b"\n")


if __name__ == "__main__":
    main()
