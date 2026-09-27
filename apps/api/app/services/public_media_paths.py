"""Path resolution for files exposed by public media endpoints."""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings


def resolve_public_media_file(path_value: str | Path | None) -> Path | None:
    """Resolve an existing regular file strictly inside the configured media root.

    Resolve symlinks before the containment check so traversal and symlinks that
    point outside the public media tree fail closed.
    """
    if not isinstance(path_value, (str, Path)):
        return None
    raw_path = str(path_value).strip()
    if not raw_path:
        return None

    try:
        media_root = Path(settings.media_root).resolve(strict=True)
        if not media_root.is_dir():
            return None
        resolved_path = Path(raw_path).resolve(strict=True)
        resolved_path.relative_to(media_root)
        if not resolved_path.is_file():
            return None
        return resolved_path
    except (OSError, RuntimeError, ValueError):
        return None
