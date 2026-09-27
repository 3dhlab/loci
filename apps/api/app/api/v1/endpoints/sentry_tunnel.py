from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import settings
from app.core.rate_limit import SENTRY_TUNNEL_RATE_LIMIT_POLICY, enforce_rate_limit

router = APIRouter(tags=["sentry"])

logger = logging.getLogger(__name__)

MAX_ENVELOPE_BYTES = 200 * 1024
DEFAULT_SENTRY_CLIENT = "loci-sentry-tunnel/1.0"


@dataclass(frozen=True)
class ParsedSentryDsn:
    scheme: str
    host: str
    path_prefix: str
    public_key: str
    secret_key: str | None
    project_id: str


def _parse_sentry_dsn(raw_dsn: str) -> ParsedSentryDsn:
    normalized = (raw_dsn or "").strip()
    if not normalized:
        raise ValueError("Sentry DSN is not configured.")

    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Sentry DSN must use http or https.")
    if not parsed.netloc:
        raise ValueError("Sentry DSN is missing the upstream host.")
    if not parsed.username:
        raise ValueError("Sentry DSN is missing the public key.")

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if not path_segments:
        raise ValueError("Sentry DSN is missing the project id.")

    project_id = path_segments[-1]
    if not project_id.isdigit():
        raise ValueError("Sentry DSN project id must be numeric.")

    path_prefix = "/".join(path_segments[:-1])
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"

    return ParsedSentryDsn(
        scheme=parsed.scheme,
        host=host,
        path_prefix=path_prefix,
        public_key=parsed.username,
        secret_key=parsed.password,
        project_id=project_id,
    )


def _configured_tunnel_dsn() -> ParsedSentryDsn:
    candidate = (settings.vite_sentry_dsn or "").strip() or (settings.sentry_dsn or "").strip()
    try:
        return _parse_sentry_dsn(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sentry tunnel is not configured.",
        ) from exc


def _parse_envelope_header(body: bytes) -> dict:
    first_line, _sep, _rest = body.partition(b"\n")
    if not first_line:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sentry envelope is empty.")

    try:
        payload = json.loads(first_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sentry envelope header is invalid.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sentry envelope header is invalid.",
        )
    return payload


def _envelope_dsn(body: bytes) -> ParsedSentryDsn:
    header = _parse_envelope_header(body)
    header_dsn = header.get("dsn")
    if not isinstance(header_dsn, str) or not header_dsn.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sentry envelope is missing the dsn header.",
        )

    try:
        return _parse_sentry_dsn(header_dsn)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _request_origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _validate_same_origin(request: Request) -> None:
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return

    if origin != _request_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin Sentry tunnel requests are not allowed.",
        )


def _build_envelope_url(dsn: ParsedSentryDsn) -> str:
    prefix = f"/{dsn.path_prefix}" if dsn.path_prefix else ""
    return f"{dsn.scheme}://{dsn.host}{prefix}/api/{dsn.project_id}/envelope/"


def _build_x_sentry_auth(dsn: ParsedSentryDsn, sentry_client: str | None) -> str:
    parts = [
        "Sentry sentry_version=7",
        f"sentry_key={dsn.public_key}",
        f"sentry_client={(sentry_client or DEFAULT_SENTRY_CLIENT).strip() or DEFAULT_SENTRY_CLIENT}",
    ]
    if dsn.secret_key:
        parts.append(f"sentry_secret={dsn.secret_key}")
    return ", ".join(parts)


@router.post("/sentry-tunnel")
async def proxy_sentry_envelope(request: Request) -> Response:
    enforce_rate_limit(request, SENTRY_TUNNEL_RATE_LIMIT_POLICY)
    _validate_same_origin(request)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_bytes = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header.",
            ) from exc
        if declared_bytes > MAX_ENVELOPE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Sentry envelope exceeds the 200KB limit.",
            )

    body = await request.body()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sentry envelope is empty.")
    if len(body) > MAX_ENVELOPE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Sentry envelope exceeds the 200KB limit.",
        )

    configured_dsn = _configured_tunnel_dsn()
    envelope_dsn = _envelope_dsn(body)
    if envelope_dsn.host != configured_dsn.host or envelope_dsn.project_id != configured_dsn.project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sentry project is not allowed.",
        )

    upstream_headers = {
        "Content-Type": request.headers.get("content-type", "application/x-sentry-envelope"),
        "X-Sentry-Auth": _build_x_sentry_auth(configured_dsn, request.headers.get("user-agent")),
        "User-Agent": request.headers.get("user-agent", DEFAULT_SENTRY_CLIENT),
    }
    content_encoding = request.headers.get("content-encoding")
    if content_encoding:
        upstream_headers["Content-Encoding"] = content_encoding

    upstream_url = _build_envelope_url(configured_dsn)

    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            upstream_response = client.post(
                upstream_url,
                content=body,
                headers=upstream_headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("Sentry tunnel upstream request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to forward Sentry envelope.",
        ) from exc

    media_type = upstream_response.headers.get("content-type") or None
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=media_type,
    )