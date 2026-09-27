"""Bound authenticated uploads before they consume unbounded local storage."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

UPLOAD_CHUNK_BYTES = 1024 * 1024
GLB_HEADER_BYTES = 12
GLB_MAGIC = b"glTF"
GLB_VERSION = 2


class UploadTooLargeError(ValueError):
    """An upload exceeded its configured byte ceiling."""


class UploadValidationError(ValueError):
    """Persisted upload bytes fail the required container contract."""


def persist_bounded_upload(source: BinaryIO, destination: Path, *, max_bytes: int) -> int:
    """Create one file exclusively and remove it after any failed write.

    The caller owns destination selection. Exclusive creation prevents concurrent
    requests from overwriting a file that another request just accepted.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    created = False
    total_bytes = 0
    try:
        with destination.open("xb") as output:
            created = True
            while chunk := source.read(UPLOAD_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadTooLargeError("upload exceeds the configured size limit")
                output.write(chunk)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise

    if total_bytes == 0:
        destination.unlink(missing_ok=True)
        raise UploadValidationError("upload is empty")
    return total_bytes


def validate_glb_v2(path: Path, *, expected_size_bytes: int) -> None:
    """Require a complete binary glTF 2 container with matching declared length."""

    if expected_size_bytes < GLB_HEADER_BYTES:
        raise UploadValidationError("GLB header is incomplete")
    try:
        with path.open("rb") as stream:
            header = stream.read(GLB_HEADER_BYTES)
    except OSError as exc:
        raise UploadValidationError("GLB file cannot be inspected") from exc

    if len(header) != GLB_HEADER_BYTES:
        raise UploadValidationError("GLB header is incomplete")
    magic, version, declared_length = struct.unpack("<4sII", header)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        raise UploadValidationError("GLB must be a binary glTF 2 container")
    if declared_length != expected_size_bytes:
        raise UploadValidationError("GLB declared length does not match the uploaded file")
