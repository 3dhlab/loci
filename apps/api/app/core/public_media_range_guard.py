"""Bound byte-range work before LOCI byte-serving endpoints run.

LOCI media players use one byte range at a time. This pure ASGI middleware
accepts that browser contract on the explicit file-serving route inventory and
returns a bodyless 400 for every other Range shape. The early boundary keeps
hostile input away from routing dependencies, object lookup, file access, and
Starlette's multipart range parser, including the quadratic merge affected by
CVE-2025-62727.

The route scope is deliberate. JSON, health, authentication, authoring-list,
and other non-file endpoints retain their existing behavior when a client sends
a Range field. Add every new FileResponse/StreamingResponse route to the
inventory and its contract test before release.
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

MAX_RANGE_HEADER_BYTES = 64
MAX_RANGE_NUMERAL_DIGITS = 19

# Complete baseline-HEAD inventory of API routes that can return file bytes.
# Parameters intentionally match one decoded path segment, including malformed
# identifiers, so unsafe Range input is rejected before validation or database
# lookup while unrelated paths fall through unchanged.
MEDIA_RANGE_GUARD_ROUTE_TEMPLATES = (
    "/api/public/objects/{website_object_id}/poster",
    "/api/public/clips/{website_clip_id}/stream",
    "/api/public/clips/{website_clip_id}/poster",
    "/api/v1/public/objects/{object_id}/model/file",
    "/api/v1/public/videos/{video_id}/stream",
    "/api/v1/public/visual-windows/{transcript_window_id}/thumbnail",
    "/api/v1/objects/{object_id}/model/file",
    "/api/v1/videos/{video_id}/stream",
    "/api/v1/clips/{clip_id}/download",
    "/api/v1/clips/{clip_id}/transcript",
    "/api/v1/search/visual-windows/{transcript_window_id}/thumbnail",
    "/api/v1/search/visual-windows/{transcript_window_id}/frames/{sample_index}",
)

_OWS = b" \t"
_RANGE_HEADER_NAME = b"range"
_BYTE_UNIT_PREFIX = b"bytes="
_REJECTION_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"content-length", b"0"),
)


def _route_template_pattern(template: str) -> re.Pattern[str]:
    pieces = template.split("/")
    pattern = "/".join(
        r"[^/]+"
        if piece.startswith("{") and piece.endswith("}")
        else re.escape(piece)
        for piece in pieces
    )
    return re.compile(rf"^{pattern}$")


_MEDIA_RANGE_GUARD_ROUTE_PATTERNS = tuple(
    _route_template_pattern(template) for template in MEDIA_RANGE_GUARD_ROUTE_TEMPLATES
)


def media_range_guard_applies_to_path(path: object) -> bool:
    """Return whether one normalized ASGI path can serve response bytes."""
    return isinstance(path, str) and any(
        pattern.fullmatch(path) for pattern in _MEDIA_RANGE_GUARD_ROUTE_PATTERNS
    )


def _is_bounded_decimal(value: bytes) -> bool:
    return (
        1 <= len(value) <= MAX_RANGE_NUMERAL_DIGITS
        and all(48 <= character <= 57 for character in value)
    )


def single_byte_range_is_supported(raw_value: bytes) -> bool:
    """Return whether one raw Range value fits LOCI's bounded browser contract."""
    if len(raw_value) > MAX_RANGE_HEADER_BYTES:
        return False

    value = raw_value.strip(_OWS)
    if (
        len(value) > MAX_RANGE_HEADER_BYTES
        or value[: len(_BYTE_UNIT_PREFIX)].lower() != _BYTE_UNIT_PREFIX
    ):
        return False

    range_spec = value[len(_BYTE_UNIT_PREFIX) :]
    if range_spec.count(b"-") != 1 or b"," in range_spec:
        return False

    start, end = range_spec.split(b"-", 1)
    if not start and not end:
        return False
    if start and not _is_bounded_decimal(start):
        return False
    if end and not _is_bounded_decimal(end):
        return False
    if start and end and int(start) > int(end):
        return False
    return True


def range_headers_are_supported(scope: Scope) -> bool:
    """Accept an absent Range header or one valid, bounded Range field value."""
    range_value: bytes | None = None
    for name, value in scope.get("headers", ()):
        if name.lower() != _RANGE_HEADER_NAME:
            continue
        if range_value is not None:
            return False
        range_value = value
    return range_value is None or single_byte_range_is_supported(range_value)


async def _send_rejection(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": list(_REJECTION_HEADERS),
        }
    )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


class PublicMediaRangeGuardMiddleware:
    """Reject unsafe GET/HEAD Range fields on byte-serving API paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "").upper() in {"GET", "HEAD"}
            and media_range_guard_applies_to_path(scope.get("path"))
            and not range_headers_are_supported(scope)
        ):
            await _send_rejection(send)
            return

        await self.app(scope, receive, send)
