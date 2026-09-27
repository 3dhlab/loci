from __future__ import annotations

import logging
import re
from importlib import import_module
from pathlib import PurePath
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_SAFE_EVENT_ID = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_SAFE_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_PYTHON_FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}\.py$")
_SAFE_EXCEPTION_TYPES = frozenset(
    {
        "ConnectionError",
        "FileNotFoundError",
        "HTTPException",
        "IntegrityError",
        "KeyError",
        "MediaProbeError",
        "MediaReviewRequired",
        "MediaValidationError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TimeoutExpired",
        "TypeError",
        "ValueError",
    }
)
_SAFE_LEVELS = frozenset({"fatal", "error", "warning", "info", "debug"})
_SAFE_ENVIRONMENTS = frozenset({"development", "test", "preview", "staging", "production"})


def _safe_text(value: object, maximum: int) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _privacy_safe_server_frame(frame: object) -> dict[str, object] | None:
    if not isinstance(frame, dict):
        return None
    raw_filename = _safe_text(frame.get("filename") or frame.get("abs_path"), 2048)
    filename = PurePath(raw_filename).name if raw_filename else ""
    if not _SAFE_PYTHON_FILENAME.fullmatch(filename):
        filename = "application.py"
    lineno = frame.get("lineno")
    return {
        "filename": filename,
        "lineno": lineno if isinstance(lineno, int) and lineno > 0 else None,
        "in_app": frame.get("in_app") if isinstance(frame.get("in_app"), bool) else None,
    }


def _privacy_safe_server_exception(exception: object) -> dict[str, object]:
    source = exception if isinstance(exception, dict) else {}
    exception_type = _safe_text(source.get("type"), 80)
    stacktrace = source.get("stacktrace") if isinstance(source.get("stacktrace"), dict) else {}
    source_frames = stacktrace.get("frames") if isinstance(stacktrace.get("frames"), list) else []
    frames = [
        safe_frame
        for frame in source_frames[-80:]
        if (safe_frame := _privacy_safe_server_frame(frame)) is not None
    ]
    mechanism = source.get("mechanism") if isinstance(source.get("mechanism"), dict) else {}
    return {
        "type": exception_type if exception_type in _SAFE_EXCEPTION_TYPES else "Error",
        "value": "Server-side exception",
        "stacktrace": {"frames": frames} if frames else None,
        "mechanism": {
            "handled": mechanism.get("handled") if isinstance(mechanism.get("handled"), bool) else None,
        }
        if mechanism
        else None,
    }


def build_privacy_safe_server_event(event: object, *, runtime: str) -> dict[str, object]:
    """Return the complete allowlisted event that may leave the API or worker."""

    source: dict[str, Any] = event if isinstance(event, dict) else {}
    event_id = _safe_text(source.get("event_id"), 64)
    level = _safe_text(source.get("level"), 24)
    environment = _safe_text(source.get("environment"), 80)
    release = _safe_text(source.get("release"), 160)
    timestamp = source.get("timestamp")
    exception_container = source.get("exception") if isinstance(source.get("exception"), dict) else {}
    exception_values = (
        exception_container.get("values") if isinstance(exception_container.get("values"), list) else []
    )
    exceptions = [_privacy_safe_server_exception(value) for value in exception_values[:4]]

    return {
        "event_id": event_id if _SAFE_EVENT_ID.fullmatch(event_id) else None,
        "timestamp": timestamp if isinstance(timestamp, (int, float)) else None,
        "platform": "python",
        "level": level if level in _SAFE_LEVELS else None,
        "environment": environment if environment in _SAFE_ENVIRONMENTS else None,
        "release": release if _SAFE_RELEASE.fullmatch(release) else None,
        "message": "Server-side exception" if source.get("message") else None,
        "transaction": "API request" if runtime == "api" else "Worker job",
        "tags": {"runtime": "api" if runtime == "api" else "worker"},
        "exception": {"values": exceptions} if exceptions else None,
    }


def configure_sentry(runtime: str) -> bool:
    dsn = settings.sentry_dsn.strip()
    if not dsn:
        return False

    try:
        sentry_sdk = import_module("sentry_sdk")
    except ImportError as error:
        logger.warning("Sentry initialization skipped because sentry-sdk is unavailable: %s", error)
        return False

    integrations: list[object] = []

    if runtime == "api":
        try:
            fastapi_module = import_module("sentry_sdk.integrations.fastapi")
            integrations.append(fastapi_module.FastApiIntegration(transaction_style="endpoint"))
        except ImportError as error:
            logger.warning("Sentry FastAPI integration skipped because sentry-sdk[fastapi] is unavailable: %s", error)
    elif runtime == "worker":
        try:
            rq_module = import_module("sentry_sdk.integrations.rq")
            integrations.append(rq_module.RqIntegration())
        except ImportError as error:
            logger.warning("Sentry RQ integration skipped because sentry-sdk[rq] is unavailable: %s", error)

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.semantic_env,
            traces_sample_rate=0.0,
            send_default_pii=False,
            max_breadcrumbs=0,
            max_request_body_size="never",
            include_local_variables=False,
            before_send=lambda event, _hint: build_privacy_safe_server_event(event, runtime=runtime),
            integrations=integrations,
        )
    except Exception as error:  # pragma: no cover - defensive logging path
        logger.warning("Sentry initialization failed: %s", error)
        return False

    return True
