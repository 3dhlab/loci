"""Sealed publication inputs in the persistent media volume.

Only an admitted operation may refer to one of these opaque UUID directories.
An existing unbound directory is never adopted as a new selection.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from app.services.publication_media_inventory import (
    _root, build_media_inventory, create_verified_media_bundle,
)
from app.services.publication_readback import canonical, digest, expected_receipt
from app.services.publication_clip_text import selected_clip_excerpts

PRIVATE_SELECTIONS = ".loci-publication-selections"
PUBLIC_GENERATIONS = ".loci-publication-generations"
MAX_METADATA = 32 * 1024 * 1024


class SelectionError(RuntimeError):
    pass


def operation_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise SelectionError("Invalid publication identity.") from None
    return value


def _open_directory(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise SelectionError("Publication storage must be private and owned.")
    return fd


def _write_new(parent, name, payload):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fchmod(stream.fileno(), 0o400)
        os.fsync(stream.fileno())


def _read_file(parent, name, *, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise SelectionError("Invalid publication seal.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(data) > limit or (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size,
                 after.st_mtime_ns, after.st_ctime_ns)):
            raise SelectionError("Publication seal changed.")
        return data


def file_digest(path, *, expected_bytes=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or
                (expected_bytes is not None and before.st_size != expected_bytes)):
            raise SelectionError("Publication bundle changed.")
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns):
            raise SelectionError("Publication bundle changed.")
        return checksum, before.st_size


@contextmanager
def verified_bundle_file(path, *, expected_bytes, expected_sha256):
    """Hash and consume one regular descriptor without reopening its pathname."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        version = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                                item.st_mtime_ns, item.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_size != expected_bytes
                or hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256
                or version(os.fstat(stream.fileno())) != version(before)):
            raise SelectionError("Generation bundle descriptor differs from its selection.")
        stream.seek(0)
        yield stream
        if version(os.fstat(stream.fileno())) != version(before):
            raise SelectionError("Generation bundle descriptor changed during consumption.")


def seal_selection(media_root, operation_id, manifest, media_paths,
                   destination_fingerprint, *, max_bytes, progress=None, expected_head=None):
    operation_uuid(operation_id)
    paths = sorted(media_paths)
    if len(paths) != len(set(paths)) or any(
            p.split("/", 1)[0] in {PRIVATE_SELECTIONS, PUBLIC_GENERATIONS} for p in paths):
        raise SelectionError("Private publication storage cannot be selected.")
    inventory = build_media_inventory(media_root, paths)
    manifest = json.loads(canonical(manifest))
    manifest["public_clip_excerpts"] = selected_clip_excerpts(media_root, manifest, inventory)
    expected_receipt(manifest, inventory)
    for model in manifest.get("object_models", []):
        item = inventory.get(model.get("public_media_relpath"))
        if item is not None and (item["sha256"] != model.get("sha256_checksum")
                                 or item["size_bytes"] != model.get("file_size_bytes")):
            raise SelectionError("Selected model differs from its approved byte identity.")
    for video in manifest.get("videos", []):
        identity = manifest.get("playback_identities", {}).get(video["id"])
        if identity is not None:
            item = inventory.get(video["public_media_relpath"])
            if item is None or any(item[k] != identity.get(k) for k in ("sha256", "size_bytes")):
                raise SelectionError("Selected playback differs from its validated byte identity.")
    if sum(row["size_bytes"] for row in inventory.values()) > max_bytes:
        raise SelectionError("The selected publication exceeds the configured byte allowance.")
    with ExitStack() as stack:
        root = _root(stack, media_root)
        base = _open_directory(root, PRIVATE_SELECTIONS, create=True)
        stack.callback(os.close, base)
        # Exclusive creation: a failed unbound capture is retained, never adopted.
        os.mkdir(operation_id, mode=0o700, dir_fd=base)
        selected = _open_directory(base, operation_id)
        stack.callback(os.close, selected)
        identity = os.fstat(selected)
        directory = Path(media_root) / PRIVATE_SELECTIONS / operation_id
        bundle = directory / "media.tar.gz"
        create_verified_media_bundle(bundle, media_root, paths, inventory, progress)
        checksum, size = file_digest(bundle)
        if size > max_bytes + max_bytes // 100 + 1024 * 1024:
            raise SelectionError("The publication bundle exceeds its allowance.")
        os.chmod(bundle, 0o400, follow_symlinks=False)
        with bundle.open("rb") as stream:
            os.fsync(stream.fileno())
        metadata = {"manifest": manifest, "media_inventory": inventory,
                    "bundle_sha256": checksum, "bundle_bytes": size,
                    "destination_fingerprint": destination_fingerprint,
                    "expected_head": expected_head if expected_head is not None else {"generation_id": None, "version": 0}}
        payload = canonical(metadata)
        if len(payload) > MAX_METADATA:
            raise SelectionError("Publication metadata exceeds its allowance.")
        selection_digest = digest(metadata)
        seal = {"version": 1, "generation_id": operation_id,
                "selection_ref": operation_id, "selection_digest": selection_digest,
                "metadata": metadata}
        _write_new(selected, "seal.json", canonical(seal))
        _write_new(selected, "manifest.json", canonical(manifest))
        with ExitStack() as check:
            fresh_root = _root(check, media_root)
            fresh_base = _open_directory(fresh_root, PRIVATE_SELECTIONS)
            check.callback(os.close, fresh_base)
            fresh = _open_directory(fresh_base, operation_id)
            check.callback(os.close, fresh)
            actual = os.fstat(fresh)
            if (actual.st_dev, actual.st_ino) != (identity.st_dev, identity.st_ino):
                raise SelectionError("Publication directory changed during capture.")
        os.fsync(selected)
        os.fchmod(selected, 0o500)
        os.fsync(base)
        return seal


def load_selection(media_root, operation_id, selection_ref, selection_digest, metadata, *, check_bundle=True):
    operation_uuid(operation_id)
    if selection_ref != operation_id or digest(metadata) != selection_digest:
        raise SelectionError("Publication selection identity changed.")
    with ExitStack() as stack:
        root = _root(stack, media_root)
        base = _open_directory(root, PRIVATE_SELECTIONS)
        stack.callback(os.close, base)
        selected = _open_directory(base, operation_id)
        stack.callback(os.close, selected)
        seal = json.loads(_read_file(selected, "seal.json", limit=MAX_METADATA + 2048))
        expected = {"version": 1, "generation_id": operation_id,
                    "selection_ref": operation_id, "selection_digest": selection_digest,
                    "metadata": metadata}
        if canonical(seal) != canonical(expected):
            raise SelectionError("Publication seal does not match its admitted operation.")
        manifest = _read_file(selected, "manifest.json", limit=MAX_METADATA)
        if manifest != canonical(metadata["manifest"]):
            raise SelectionError("Publication manifest changed after admission.")
        bundle = Path(media_root) / PRIVATE_SELECTIONS / operation_id / "media.tar.gz"
        if check_bundle:
            checksum, _ = file_digest(bundle, expected_bytes=metadata["bundle_bytes"])
            if checksum != metadata["bundle_sha256"]:
                raise SelectionError("Publication bundle changed after admission.")
        return {"bundle": bundle, "manifest": bundle.with_name("manifest.json"), "seal": seal}


@contextmanager
def opened_verified_bundle(media_root, operation_id, selection_ref, selection_digest, metadata):
    """Keep the verified descriptor open through transfer; never reopen by path."""
    load_selection(media_root, operation_id, selection_ref, selection_digest, metadata, check_bundle=False)
    with ExitStack() as stack:
        root = _root(stack, media_root)
        base = _open_directory(root, PRIVATE_SELECTIONS)
        stack.callback(os.close, base)
        selected = _open_directory(base, operation_id)
        stack.callback(os.close, selected)
        directory = os.fstat(selected)
        fd = os.open("media.tar.gz", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=selected)
        stream = stack.enter_context(os.fdopen(fd, "rb"))
        before = os.fstat(stream.fileno())
        version = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                                 value.st_mtime_ns, value.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_size != metadata["bundle_bytes"]
                or hashlib.file_digest(stream, "sha256").hexdigest() != metadata["bundle_sha256"]
                or version(os.fstat(stream.fileno())) != version(before)):
            raise SelectionError("Publication transfer descriptor does not match its seal.")
        stream.seek(0)
        yield stream
        if version(os.fstat(stream.fileno())) != version(before):
            raise SelectionError("Publication transfer descriptor changed.")
        # Detect replacement of any ancestor even if the open old descriptor
        # still names the correct bytes; no replacement namespace is adopted.
        with ExitStack() as fresh:
            root_again = _root(fresh, media_root)
            base_again = _open_directory(root_again, PRIVATE_SELECTIONS)
            fresh.callback(os.close, base_again)
            selected_again = _open_directory(base_again, operation_id)
            fresh.callback(os.close, selected_again)
            current = os.fstat(selected_again)
            if (current.st_dev, current.st_ino) != (directory.st_dev, directory.st_ino):
                raise SelectionError("Publication transfer namespace changed.")
