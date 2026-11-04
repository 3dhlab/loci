"""Install immutable generation files without changing the current public paths."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tarfile
import re

from app.services.publication_media_inventory import _path, _root, build_media_inventory
from app.services.publication_readback import PATH_FIELDS, canonical, digest, expected_receipt
from app.services.publication_selection_store import (
    MAX_METADATA, PUBLIC_GENERATIONS, SelectionError, _open_directory, _write_new,
    operation_uuid, verified_bundle_file,
)


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size > 65536:
            raise SelectionError("Generation archive metadata is too large.")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 65536:
            raise SelectionError("Generation archive metadata is too large.")
        return super()._proc_gnulong(archive)


def selected_metadata(selection_digest, metadata):
    if (not isinstance(metadata, dict) or set(metadata) != {"manifest", "media_inventory", "bundle_sha256",
            "bundle_bytes", "destination_fingerprint", "expected_head"} or type(metadata.get("bundle_bytes")) is not int
            or metadata["bundle_bytes"] < 0
            or not isinstance(metadata.get("bundle_sha256"), str)
            or re.fullmatch("[0-9a-f]{64}", metadata["bundle_sha256"]) is None
            or not isinstance(metadata.get("destination_fingerprint"), str)
            or re.fullmatch("[0-9a-f]{64}", metadata["destination_fingerprint"]) is None
            or digest(metadata) != selection_digest):
        raise SelectionError("Generation metadata does not match its commitment.")
    if len(canonical(metadata)) > MAX_METADATA:
        raise SelectionError("Generation metadata is too large.")
    head = metadata.get("expected_head")
    if (not isinstance(head, dict) or set(head) != {"generation_id", "version"}
            or type(head["version"]) is not int or head["version"] < 0):
        raise SelectionError("Generation predecessor is invalid.")
    if head["generation_id"] is not None:
        operation_uuid(head["generation_id"])
    return expected_receipt(metadata["manifest"], metadata["media_inventory"])


def generation_root(media_root, operation_id):
    operation_uuid(operation_id)
    return Path(media_root) / PUBLIC_GENERATIONS / operation_id / "files"


def install_generation(media_root, operation_id, selection_digest, metadata, bundle):
    """Exclusive fresh installation; orphan directories require reconciliation.

    No extractall, links, devices, duplicates, unknown paths or overwrites. An
    interrupted install remains private/unreferenced; it is never auto-adopted.
    The DB transaction owns the later switch of row references and head receipt.
    """
    operation_uuid(operation_id)
    selected_metadata(selection_digest, metadata)
    inventory = metadata["media_inventory"]
    with verified_bundle_file(bundle, expected_bytes=metadata["bundle_bytes"],
                              expected_sha256=metadata["bundle_sha256"]) as supplied, ExitStack() as stack:
        root = _root(stack, media_root)
        base = _open_directory(root, PUBLIC_GENERATIONS, create=True)
        stack.callback(os.close, base)
        os.mkdir(operation_id, mode=0o700, dir_fd=base)
        generation = _open_directory(base, operation_id)
        stack.callback(os.close, generation)
        _write_new(generation, "seal.json", canonical({"version": 1,
                   "operation_id": operation_id, "selection_digest": selection_digest,
                   "metadata": metadata}))
        os.mkdir("files", mode=0o700, dir_fd=generation)
        files = _open_directory(generation, "files")
        stack.callback(os.close, files)
        seen = set()
        with tarfile.open(fileobj=supplied, mode="r:gz", tarinfo=_BoundedTarInfo) as archive:
            for member in archive:
                if (not member.isfile() or member.name in seen or member.name not in inventory
                        or member.size != inventory[member.name]["size_bytes"]):
                    raise SelectionError("Uploaded generation member is invalid.")
                parts = _path(member.name)
                with ExitStack() as parents:
                    parent = files
                    for part in parts[:-1]:
                        parent = _open_directory(parent, part, create=True)
                        parents.callback(os.close, parent)
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
                    with os.fdopen(fd, "wb") as target:
                        source = archive.extractfile(member)
                        if source is None:
                            raise SelectionError("Uploaded generation member is missing.")
                        with source:
                            total = 0
                            checksum = hashlib.sha256()
                            while True:
                                data = source.read(min(1024 * 1024, member.size - total + 1))
                                if not data:
                                    break
                                total += len(data)
                                if total > member.size:
                                    raise SelectionError("Uploaded generation member exceeds its size.")
                                checksum.update(data)
                                target.write(data)
                        if total != member.size or checksum.hexdigest() != inventory[member.name]["sha256"]:
                            raise SelectionError("Uploaded generation member differs from its selected bytes.")
                        target.flush()
                        os.fchmod(target.fileno(), 0o400)
                        os.fsync(target.fileno())
                    os.fsync(parent)
                seen.add(member.name)
        if seen != set(inventory):
            raise SelectionError("Uploaded generation is incomplete.")
        os.fsync(files)
        os.fsync(generation)
        os.fsync(base)
    result = generation_root(media_root, operation_id)
    observed = build_media_inventory(result, sorted(inventory))
    if any(observed[n][k] != inventory[n][k] for n in inventory for k in ("size_bytes", "sha256")):
        raise SelectionError("Installed generation failed readback.")
    return result


def generation_manifest(manifest, operation_id):
    """Hydrate existing import paths into a new immutable generation namespace."""
    operation_uuid(operation_id)
    result = json.loads(canonical(manifest))
    prefix = f"{PUBLIC_GENERATIONS}/{operation_id}/files/"
    for collection, aliases in PATH_FIELDS.items():
        for row in result.get(collection, []):
            for field in aliases:
                if row.get(field) is not None:
                    _path(row[field])
                    row[field] = prefix + row[field]
    for row in result.get("visual_window_descriptions", []):
        frames = row.get("frame_manifest_json")
        if isinstance(frames, dict):
            for item in [frames.get("thumbnail"), *(frames.get("sample_frames") or [])]:
                if isinstance(item, dict) and item.get("path") is not None:
                    _path(item["path"])
                    item["path"] = prefix + item["path"]
    for row in result.get("clips", []):
        if row.get("public_poster_relpath") is not None:
            _path(row["public_poster_relpath"])
            row["public_poster_relpath"] = prefix + row["public_poster_relpath"]
    result["media_paths"] = [prefix + p for p in manifest.get("media_paths", [])]
    return result
