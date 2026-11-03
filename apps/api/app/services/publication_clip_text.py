"""Freeze the existing public clip excerpt rendering from selected file bytes."""
from contextlib import ExitStack
import hashlib
import os

from app.services.publication_media_inventory import MediaInventoryError, _path, _root, _stable_file

MAX_PUBLIC_CLIP_TEXT_BYTES = 1024 * 1024
MAX_PUBLIC_CLIP_TEXT_TOTAL_BYTES = 32 * 1024 * 1024


def selected_clip_excerpts(media_root, manifest, inventory):
    result = {}
    total_text = 0
    def save(ident, text):
        nonlocal total_text
        if text is not None:
            total_text += len(text.encode("utf-8"))
        if total_text > MAX_PUBLIC_CLIP_TEXT_TOTAL_BYTES:
            raise MediaInventoryError("Selected public clip text exceeds the combined allowance.")
        result[ident] = text
    for clip in manifest.get("clips", []):
        explicit = clip.get("transcript_excerpt")
        if isinstance(explicit, str) and explicit.strip():
            if len(explicit.encode("utf-8")) > MAX_PUBLIC_CLIP_TEXT_BYTES:
                raise MediaInventoryError("Public clip text exceeds its allowance.")
            save(clip["id"], explicit.strip())
            continue
        relative = clip.get("public_transcript_relpath")
        if relative is None:
            save(clip["id"], None)
            continue
        expected = inventory.get(relative)
        if not expected or expected["size_bytes"] > MAX_PUBLIC_CLIP_TEXT_BYTES:
            raise MediaInventoryError("Selected public clip text is unavailable or too large.")
        with ExitStack() as opened:
            identities = []
            root = _root(opened, media_root, identities)
            with _stable_file(root, identities, media_root, _path(relative)) as (fd, before):
                if before.st_size != expected["size_bytes"]:
                    raise MediaInventoryError("Selected public clip text changed.")
                chunks = []
                total = 0
                checksum = hashlib.sha256()
                while True:
                    chunk = os.read(fd, min(65536, MAX_PUBLIC_CLIP_TEXT_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PUBLIC_CLIP_TEXT_BYTES:
                        raise MediaInventoryError("Public clip text exceeds its allowance.")
                    chunks.append(chunk)
                    checksum.update(chunk)
                if total != expected["size_bytes"] or checksum.hexdigest() != expected["sha256"]:
                    raise MediaInventoryError("Selected public clip text changed.")
                try:
                    text = b"".join(chunks).decode("utf-8").strip()
                except UnicodeError:
                    raise MediaInventoryError("Selected public clip text is not UTF-8.") from None
        save(clip["id"], text or None)
    return result
