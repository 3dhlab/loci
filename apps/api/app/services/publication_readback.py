"""Destination database/media readback contracts for the legacy SSH transport.

A receipt proves a point-in-time database/file observation by the configured SSH
destination. It does not prove HTTP delivery, durable worker execution or an
atomic database/media switch. Callers must keep those release gates separate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import hashlib
import json
import math
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
import re
from uuid import UUID


class PublicationReadbackError(RuntimeError):
    pass


COLLECTIONS = (
    "projects", "objects", "videos", "transcripts", "segments",
    "transcript_windows", "visual_window_descriptions", "object_models",
    "clips", "corpus_phrases", "object_model_annotations",
)
IGNORED_FIELDS = {"embedding_vector"}
PATH_FIELDS = {
    "videos": {"public_media_relpath": "source_path"},
    "object_models": {"public_media_relpath": "storage_path",
                      "public_poster_relpath": "public_poster_path"},
    "clips": {"public_media_relpath": "output_mp4_path",
              "public_transcript_relpath": "metadata_json_path"},
    "visual_window_descriptions": {"thumbnail_relpath": "thumbnail_path"},
}
SIX_PLACES = {"position_x", "position_y", "position_z", "rotation_x", "rotation_y",
              "rotation_z", "point_x", "point_y", "point_z", "normal_x", "normal_y", "normal_z"}


def canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise PublicationReadbackError("Publication readback value is invalid.") from None


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _relative(value, root):
    if value is None:
        return None
    try:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError()
        return path.relative_to(Path(root)).as_posix()
    except (TypeError, ValueError):
        raise PublicationReadbackError("Destination media path is invalid.") from None


def _value(value, field):
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, UUID):
        return str(value)
    if field in {"created_at", "updated_at"}:
        try:
            instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError()
            return instant.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, AttributeError):
            raise PublicationReadbackError("Destination timestamp is invalid.") from None
    if field in SIX_PLACES or field == "corpus_density":
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError()
            return str(number.quantize(Decimal("0.0001" if field == "corpus_density" else "0.000001"),
                                       rounding=ROUND_HALF_UP))
        except (ValueError, ArithmeticError):
            raise PublicationReadbackError("Destination numeric value is invalid.") from None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise PublicationReadbackError("Destination numeric value is invalid.")
    return value


def _frame_paths(value, root):
    if value is None:
        return None
    copy = json.loads(canonical(value))
    if not isinstance(copy, dict):
        raise PublicationReadbackError("Destination frame manifest is invalid.")
    entries = [copy.get("thumbnail")] + list(copy.get("sample_frames") or [])
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") is not None:
            entry["path"] = _relative(entry["path"], root)
    return copy


def record_contract(manifest: dict, observed: dict | None = None, *, media_root=None) -> dict:
    """Compare all selected imported fields, excluding search embedding vectors.

    Explicit aliases undo destination media-path hydration. SQL Numeric fields
    use their schema scale. A clip poster is file-derived, not a Clip column,
    and is checked through the separate byte inventory. Row sets must match;
    extra destination rows and silently skipped annotations are errors.
    """
    result = {}
    for collection in COLLECTIONS:
        rows = manifest.get(collection, [])
        if not isinstance(rows, list):
            raise PublicationReadbackError("Publication row collection is invalid.")
        expected_ids = [row.get("id") for row in rows if isinstance(row, dict)]
        if (len(expected_ids) != len(rows) or any(not isinstance(value, str) for value in expected_ids)
                or len(set(expected_ids)) != len(expected_ids)):
            raise PublicationReadbackError("Publication row identity is invalid.")
        actual = observed.get(collection, {}) if observed is not None else None
        if observed is not None and not isinstance(actual, dict):
            raise PublicationReadbackError("Destination row collection is invalid.")
        if actual is not None and set(actual) != set(expected_ids):
            raise PublicationReadbackError("Destination row selection does not match.")
        output = []
        for row in rows:
            item = {}
            for field, value in row.items():
                if (field in IGNORED_FIELDS
                        or (collection == "corpus_phrases" and field in {"created_at", "updated_at"})
                        or (collection == "clips" and field == "public_poster_relpath")):
                    continue
                if actual is not None:
                    source = actual[row["id"]]
                    attribute = PATH_FIELDS.get(collection, {}).get(field, field)
                    if attribute not in source:
                        raise PublicationReadbackError("Destination field is missing.")
                    value = source[attribute]
                    if field in PATH_FIELDS.get(collection, {}):
                        value = _relative(value, media_root)
                    elif field == "frame_manifest_json":
                        value = _frame_paths(value, media_root)
                if field == "website_relations_json" and not value:
                    value = {"publication_ids": [], "project_ids": [], "location_ids": []}
                item[field] = _value(value, field)
            output.append(item)
        result[collection] = sorted(output, key=lambda row: row["id"])
    return result


def media_contract(inventory: dict) -> dict:
    result = {}
    if not isinstance(inventory, dict):
        raise PublicationReadbackError("Publication media inventory is invalid.")
    for path, item in inventory.items():
        if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or not isinstance(item, dict) or type(item.get("size_bytes")) is not int
                or item["size_bytes"] < 0 or not isinstance(item.get("sha256"), str)
                or re.fullmatch("[0-9a-f]{64}", item["sha256"]) is None):
            raise PublicationReadbackError("Publication media identity is invalid.")
        result[path] = {"size_bytes": item["size_bytes"], "sha256": item["sha256"]}
    return result


def expected_receipt(manifest: dict, inventory: dict) -> dict:
    records = record_contract(manifest)
    media = media_contract(inventory)
    paths = manifest.get("media_paths", [])
    if (not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
            or len(paths) != len(set(paths)) or set(paths) != set(media)):
        raise PublicationReadbackError("Publication media selection does not match.")
    return {"version": 1, "records_sha256": digest(records), "media_sha256": digest(media),
            "selection_sha256": digest({"records": records, "media": media}),
            "row_counts": {name: len(rows) for name, rows in records.items()},
            "media_file_count": len(media)}


def observe_receipt(manifest, expected_inventory, observed_rows, observed_inventory, *, media_root):
    expected = expected_receipt(manifest, expected_inventory)
    records = record_contract(manifest, observed_rows, media_root=media_root)
    media = media_contract(observed_inventory)
    if digest(records) != expected["records_sha256"] or digest(media) != expected["media_sha256"]:
        raise PublicationReadbackError("Destination record or media readback does not match.")
    return expected


def require_receipt(payload, expected, challenge):
    if (not isinstance(payload, dict) or set(payload) != {"receipt", "challenge"}
            or payload["challenge"] != challenge or canonical(payload["receipt"]) != canonical(expected)):
        raise PublicationReadbackError("Destination receipt is missing, stale or mismatched.")
    return payload["receipt"]


def bounded_readback_command(command: list[str], request: bytes, *, timeout=1200) -> bytes:
    """Bound destination stdout while supplying a finite private stdin file."""
    with tempfile.TemporaryFile() as supplied:
        supplied.write(request)
        supplied.seek(0)
        with subprocess.Popen(command, stdin=supplied, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as child:
            try:
                result = bytearray()
                end = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Destination verification timed out.")
                        if not selector.select(min(remaining, 1)):
                            continue
                        data = os.read(child.stdout.fileno(), min(8192, 65537 - len(result)))
                        if not data:
                            break
                        result.extend(data)
                        if len(result) > 65536:
                            raise ValueError("Destination verification response is too large.")
                remaining = end - time.monotonic()
                if remaining <= 0 or child.wait(timeout=remaining) != 0:
                    raise ValueError("Destination verification did not complete.")
                return bytes(result)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
