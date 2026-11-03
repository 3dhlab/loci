"""Read the committed destination rows and bytes, without importing or changing them."""
import argparse
import json
import re
import sys

from sqlalchemy import inspect, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import entities
from app.services.publication_media_inventory import build_media_inventory
from app.services.publication_readback import (
    IGNORED_FIELDS, PATH_FIELDS, PublicationReadbackError, canonical,
    expected_receipt, observe_receipt,
)

MODELS = {
    "projects": entities.Project, "objects": entities.Object, "videos": entities.Video,
    "transcripts": entities.Transcript, "segments": entities.Segment,
    "transcript_windows": entities.TranscriptWindow,
    "visual_window_descriptions": entities.VisualWindowDescription,
    "object_models": entities.ObjectModel, "clips": entities.Clip,
    "corpus_phrases": entities.CorpusPhrase,
    "object_model_annotations": entities.ObjectModelAnnotation,
}


def observe_database(db, manifest):
    observed = {}
    for collection, model in MODELS.items():
        rows = manifest.get(collection, [])
        columns = {column.key: getattr(model, column.key) for column in inspect(model).column_attrs}
        names = {"id"}
        for row in rows:
            for field in row:
                if (field in IGNORED_FIELDS
                        or (collection == "corpus_phrases" and field in {"created_at", "updated_at"})
                        or (collection == "clips" and field == "public_poster_relpath")):
                    continue
                names.add(PATH_FIELDS.get(collection, {}).get(field, field))
        if not names <= columns.keys():
            raise PublicationReadbackError("Unsupported destination field.")
        # The request bounds the expected row count. One extra row is enough
        # to reject an unexpected selection, without materializing a whole
        # unrelated table or any excluded search embedding vectors.
        selected = [columns[name].label(name) for name in sorted(names)]
        result = db.execute(select(*selected).limit(len(rows) + 1)).mappings()
        observed[collection] = {
            str(row["id"]): dict(row) for row in result
        }
    return observed


def verify(payload):
    if not isinstance(payload, dict) or set(payload) != {"manifest", "media_inventory", "challenge"}:
        raise ValueError("Invalid readback request.")
    if not isinstance(payload["challenge"], str) or re.fullmatch("[0-9a-f]{64}", payload["challenge"]) is None:
        raise ValueError("Invalid readback challenge.")
    manifest = payload["manifest"]
    expected_receipt(manifest, payload["media_inventory"])
    # A new repeatable-read transaction observes committed destination state,
    # independently of the import transaction and uploaded manifest file.
    with SessionLocal() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
        observed = observe_database(db, manifest)
        inventory = build_media_inventory(settings.media_root, list(payload["media_inventory"]))
        receipt = observe_receipt(manifest, payload["media_inventory"], observed, inventory,
                                  media_root=settings.media_root)
    return {"receipt": receipt, "challenge": payload["challenge"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-version", action="store_true")
    args = parser.parse_args()
    if args.protocol_version:
        sys.stdout.buffer.write(canonical({"protocol": "loci-publication-readback", "version": 1}) + b"\n")
        return
    # The caller supplies the expected package through stdin, not a mutable
    # shared destination filename. No transport credentials are included.
    raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Readback request is too large.")
    result = verify(json.loads(raw))
    sys.stdout.buffer.write(canonical(result) + b"\n")


if __name__ == "__main__":
    main()
