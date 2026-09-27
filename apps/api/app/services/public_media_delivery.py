"""Conditional delivery for large public model and video representations.

The public endpoints provide the persisted SHA-256 for the exact selected body.
That digest is a strong validator without reading the media file during a request.
Legacy video rows without persisted playback identity receive an explicitly weak
metadata validator and a no-store policy until a gated backfill records the digest.

Starlette's ``FileResponse`` supplies byte ranges.  This module adds conditional
GET/HEAD handling and constrains ``If-Range`` to strong representation identity.
The global public-media Range guard rejects unsupported Range syntax before this
response is constructed.
"""

from __future__ import annotations

import stat
from email.utils import formatdate
from pathlib import Path

from fastapi.responses import FileResponse
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from app.services.public_media_cache import normalized_sha256

LEGACY_MEDIA_CACHE_CONTROL = "no-store, max-age=0, must-revalidate"


class MediaIdentityUnavailableError(RuntimeError):
    """The persisted representation identity cannot safely describe this file."""


class MediaValidators:
    """Validator metadata for one representation, derived from one ``stat``."""

    __slots__ = ("etag", "is_strong", "last_modified", "stat_result")

    def __init__(
        self,
        path: Path,
        *,
        content_sha256: str | None,
        expected_size_bytes: int | None = None,
        allow_legacy_weak: bool = False,
        require_expected_size: bool = False,
    ):
        try:
            self.stat_result = path.stat()
        except OSError as exc:
            raise MediaIdentityUnavailableError("representation file cannot be inspected") from exc
        if not stat.S_ISREG(self.stat_result.st_mode):
            raise MediaIdentityUnavailableError("representation path is not a regular file")
        digest = normalized_sha256(content_sha256)

        if digest is not None:
            if require_expected_size and expected_size_bytes is None:
                raise MediaIdentityUnavailableError("persisted representation size is missing")
            if expected_size_bytes is not None and expected_size_bytes <= 0:
                raise MediaIdentityUnavailableError("persisted representation size is invalid")
            if expected_size_bytes is not None and expected_size_bytes != self.stat_result.st_size:
                raise MediaIdentityUnavailableError("persisted representation size does not match the file")
            # The complete persisted SHA-256 identifies the exact selected body.
            self.etag = f'"sha256:{digest}"'
            self.is_strong = True
        else:
            # A null pair identifies an intentionally unmigrated video row. Any
            # partial or malformed persisted identity is an integrity failure.
            legacy_identity = content_sha256 is None and expected_size_bytes is None
            if not allow_legacy_weak or not legacy_identity:
                raise MediaIdentityUnavailableError("persisted representation digest is unavailable")
            self.etag = (
                f'W/"legacy-{self.stat_result.st_size:x}-'
                f'{self.stat_result.st_mtime_ns:x}"'
            )
            self.is_strong = False

        self.last_modified = formatdate(self.stat_result.st_mtime, usegmt=True)


class _RepresentationFileResponse(FileResponse):
    """FileResponse whose If-Range policy follows validator strength."""

    def __init__(self, *args, allow_if_range: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self._allow_if_range = allow_if_range

    def _should_use_range(self, http_if_range: str) -> bool:
        # RFC 9110 section 13.1.5 requires a strong entity-tag for If-Range.
        # LOCI treats every date-form If-Range as a mismatch: a filesystem mtime
        # rounded to HTTP-date seconds cannot prove byte-for-byte continuity
        # across an artifact replacement within the same second.
        if not self._allow_if_range or http_if_range.lstrip().startswith("W/"):
            return False
        return http_if_range == self.headers["etag"]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_compliant_416(message: dict) -> None:
            # Keep unsatisfied responses cache-disabled. The Content-Range
            # normalization also preserves protocol correctness if this module
            # runs briefly against an older dependency during rollback.
            if message["type"] == "http.response.start" and message["status"] == 416:
                headers = []
                has_cache_control = False
                for name, value in message.get("headers", []):
                    lowered = name.lower()
                    if lowered == b"content-range" and value.startswith(b"*/"):
                        value = b"bytes " + value
                    if lowered == b"cache-control":
                        has_cache_control = True
                    headers.append((name, value))
                if not has_cache_control:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await super().__call__(scope, receive, send_with_compliant_416)


def _etag_matches(header_value: str, etag: str) -> bool:
    """Apply the weak comparison RFC 9110 requires for If-None-Match."""
    candidates = [item.strip() for item in header_value.split(",") if item.strip()]
    if "*" in candidates:
        return True
    bare_etag = etag.removeprefix("W/")
    return any(candidate.removeprefix("W/") == bare_etag for candidate in candidates)


def conditional_media_response(
    request: Request,
    path: Path,
    *,
    media_type: str,
    filename: str | None,
    cache_control: str,
    content_sha256: str | None,
    expected_size_bytes: int | None = None,
    allow_legacy_weak: bool = False,
    require_expected_size: bool = False,
) -> Response:
    """Serve one selected representation with Range and conditional support.

    Persisted strong identity keeps validation O(1) for large artifacts. Legacy
    weak identity remains range-capable for ordinary playback; every ``If-Range``
    request receives the complete 200 representation because a weak validator
    cannot authorize byte-range resumption.
    """
    validators = MediaValidators(
        path,
        content_sha256=content_sha256,
        expected_size_bytes=expected_size_bytes,
        allow_legacy_weak=allow_legacy_weak,
        require_expected_size=require_expected_size,
    )
    effective_cache_control = (
        cache_control if validators.is_strong else LEGACY_MEDIA_CACHE_CONTROL
    )
    representation_headers = {
        "ETag": validators.etag,
        "Last-Modified": validators.last_modified,
        "Cache-Control": effective_cache_control,
        "Accept-Ranges": "bytes",
    }

    # The persisted content digest is the authoritative validator. Filesystem
    # mtime has finer internal resolution than an HTTP-date, so date comparison
    # could answer a false 304 after replacement within the same second.
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None and _etag_matches(if_none_match, validators.etag):
        return Response(status_code=304, headers=representation_headers)

    return _RepresentationFileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        stat_result=validators.stat_result,
        headers=representation_headers,
        allow_if_range=validators.is_strong,
    )
