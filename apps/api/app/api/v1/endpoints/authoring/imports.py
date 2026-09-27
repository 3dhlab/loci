"""Guarded import controls for the private authoring lane (Stage B4).

This module holds the PURE, DB-less, unit-testable core of the guarded import
tier: import-root path confinement, source_version shape + project-wide
uniqueness, fixture/media SHA comparison, the preflight checklist builder, and
the rollback-scope constructor. The FastAPI routes in ``routes.py`` orchestrate
these helpers, persist ``import_run`` / ``import_run_check`` rows, and write the
single audit event per state transition.

Hard boundaries (enforced by design here, and by the routes that call this):
- No route in this lane calls OCR/OpenAI.
- No route widens the public citation payload — the allowed-key set below is the
  same one the readiness gate enforces.
- Real production import/rollback is gated behind an explicit environment flag
  and the ``operator`` role; until then every run is dry-run and touches no
  production data. The executor is an injectable seam so tests never import.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable

# source_version shape (mirrors the readiness snapshot validator).
SOURCE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The public citation payload allow-list. A preflight that would surface any key
# outside this set fails the public-contract dry check. Kept in sync with
# ``app.services.public_evidence._citation_attribution_payload``.
PUBLIC_CITATION_ALLOWED_KEYS = frozenset(
    {"speaker_label", "session_date", "session_date_text", "session_date_precision", "attribution_mode"}
)

# Default import-root the staged fixture must live under. Overridable by env so
# the same confinement rule holds in local, CI, and hosted deployments.
DEFAULT_IMPORT_ROOT = os.environ.get("AUTHORING_IMPORT_ROOT", "/var/lib/semantic/import")

# Where pre-import backups are written (confined, server-side).
DEFAULT_BACKUP_ROOT = os.environ.get("AUTHORING_IMPORT_BACKUP_ROOT", "/var/lib/semantic/import/backups")


def production_imports_enabled() -> bool:
    """Whether real (row-writing) import execution is permitted.

    Read live (not at import time) so the gate can be toggled per environment /
    per test without import-order coupling. OFF unless explicitly enabled.
    """
    return os.environ.get("AUTHORING_IMPORT_PRODUCTION_ENABLED", "").lower() in {"1", "true", "yes"}


# Back-compat module constant (snapshot of the flag at import time). Prefer the
# function above for any runtime decision.
PRODUCTION_IMPORTS_ENABLED = production_imports_enabled()


def compute_sha256(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    """Stream a file and return its lowercase hex SHA-256.

    Used to recompute media hashes server-side at execution time so the imported
    bytes are verified against the approved readiness binding rather than
    trusting an operator-supplied value. For the fixture, prefer
    :func:`sha256_bytes` on bytes read once (single-read hardening).
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Lowercase hex SHA-256 of an in-memory byte string."""
    return hashlib.sha256(data).hexdigest()


class PathConfinementError(ValueError):
    """Raised when a fixture path escapes the allowlisted import root."""


def validate_source_version(value: str | None) -> bool:
    """True when ``value`` matches the canonical source_version shape."""
    return bool(value) and bool(SOURCE_VERSION_RE.match(value))


def source_version_available(existing_versions: set[str] | frozenset[str], candidate: str) -> bool:
    """True when ``candidate`` is not already reserved by another binding.

    ``existing_versions`` is the set of source_versions already bound in the
    project (excluding the binding being validated). Project-wide uniqueness is
    the guard against two different content bundles colliding on one version.
    """
    return candidate not in set(existing_versions)


def sha_matches(expected: str | None, actual: str | None) -> bool:
    """Case-insensitive SHA comparison; False if either side is missing."""
    if not expected or not actual:
        return False
    return expected.strip().lower() == actual.strip().lower()


def confine_import_path(candidate: str | os.PathLike[str], import_root: str | os.PathLike[str] | None = None) -> Path:
    """Resolve ``candidate`` and assert it lives inside ``import_root``.

    Rejects traversal (``..``), absolute escapes, and symlink-style escapes by
    fully resolving both sides and requiring containment. Returns the resolved
    path on success; raises :class:`PathConfinementError` otherwise. This does
    not require the path to exist (so it is unit-testable without a fixture).
    """
    root = Path(import_root or DEFAULT_IMPORT_ROOT)
    # Resolve symlinks/.. without requiring existence (strict=False).
    resolved_root = root.resolve(strict=False)
    resolved = (resolved_root / candidate).resolve(strict=False) if not Path(candidate).is_absolute() \
        else Path(candidate).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PathConfinementError(
            f"fixture path escapes import root: {candidate!r} not under {resolved_root}"
        ) from exc
    return resolved


def rollback_scope(video_uuid: Any, source_version: str) -> dict[str, str]:
    """The exact, minimal rollback scope: video_id + source_version only.

    A guarded rollback deletes only rows matching BOTH keys; this constructor is
    the single source of truth for that scope so no caller can widen it.
    """
    return {"video_id": str(video_uuid), "source_version": source_version}


def build_preflight_checks(
    *,
    snapshot_exists: bool,
    snapshot_passed: bool,
    recomputed_digest: str | None,
    snapshot_digest: str | None,
    source_version: str,
    existing_source_versions: set[str] | frozenset[str],
    target_sv_rows: int,
    fixture_sha_expected: str | None,
    fixture_sha_actual: str | None,
    media_present: bool,
    media_sha_expected: str | None,
    media_sha_actual: str | None,
    fixture_path_confined: bool,
    backup_status: str,
    public_contract_keys_ok: bool,
    mode: str,
) -> list[dict[str, Any]]:
    """Construct the ordered preflight checklist.

    Each entry is ``{"name", "status": pass|fail|info, "detail", "required"}``.
    A required check that is not ``pass`` blocks import (see
    :func:`preflight_passed`). ``media_sha`` is required only when media is
    present; ``backup_captured`` is required only for production mode (in
    dry-run a ``pending`` backup is acceptable and recorded as ``info``).
    """
    digest_match = bool(recomputed_digest and snapshot_digest and recomputed_digest == snapshot_digest)
    checks: list[dict[str, Any]] = [
        {
            "name": "readiness_snapshot_present_and_green",
            "status": "pass" if (snapshot_exists and snapshot_passed) else "fail",
            "detail": "readiness snapshot exists and all readiness checks passed"
            if (snapshot_exists and snapshot_passed)
            else ("snapshot present but readiness not green" if snapshot_exists else "no readiness snapshot"),
            "required": True,
        },
        {
            "name": "readiness_digest_matches_snapshot",
            "status": "pass" if digest_match else "fail",
            "detail": "recomputed readiness digest equals the snapshot's bound digest"
            if digest_match
            else "readiness content changed since the snapshot (digest mismatch)",
            "required": True,
        },
        {
            "name": "source_version_shape_valid",
            "status": "pass" if validate_source_version(source_version) else "fail",
            "detail": f"source_version={source_version!r}",
            "required": True,
        },
        {
            "name": "source_version_unique_for_project",
            "status": "pass" if source_version_available(existing_source_versions, source_version) else "fail",
            "detail": "source_version is not bound to another content bundle in this project"
            if source_version_available(existing_source_versions, source_version)
            else "source_version already bound to a different bundle",
            "required": True,
        },
        {
            "name": "target_sv_rows_zero",
            "status": "pass" if target_sv_rows == 0 else "fail",
            "detail": f"{target_sv_rows} existing rows for this source_version (must be 0)",
            "required": True,
        },
        {
            "name": "fixture_sha_matches_snapshot",
            "status": "pass" if sha_matches(fixture_sha_expected, fixture_sha_actual) else "fail",
            "detail": "fixture SHA-256 matches the readiness snapshot binding"
            if sha_matches(fixture_sha_expected, fixture_sha_actual)
            else "fixture SHA-256 missing or does not match snapshot",
            "required": True,
        },
        {
            "name": "fixture_path_confined",
            "status": "pass" if fixture_path_confined else "fail",
            "detail": "staged fixture path is inside the allowlisted import root"
            if fixture_path_confined
            else "fixture path escapes the import root",
            "required": True,
        },
        {
            "name": "public_contract_allowed_keys_only",
            "status": "pass" if public_contract_keys_ok else "fail",
            "detail": "public preview exposes only allowed citation keys",
            "required": True,
        },
        {
            "name": "media_sha_matches_batch",
            "status": ("pass" if sha_matches(media_sha_expected, media_sha_actual) else "fail")
            if media_present
            else "info",
            "detail": (
                "media SHA-256 matches the batch source media"
                if (media_present and sha_matches(media_sha_expected, media_sha_actual))
                else ("media SHA mismatch" if media_present else "no batch media path recorded; media SHA check skipped")
            ),
            "required": media_present,
        },
        {
            "name": "backup_captured",
            "status": "pass" if backup_status == "captured" else ("info" if mode != "production" else "fail"),
            "detail": (
                "pre-import backup captured"
                if backup_status == "captured"
                else (f"backup pending (acceptable in {mode} mode)" if mode != "production" else "backup required for production import")
            ),
            "required": mode == "production",
        },
    ]
    return checks


def preflight_passed(checks: list[dict[str, Any]]) -> bool:
    """True only when every REQUIRED check has status ``pass``."""
    return all(c["status"] == "pass" for c in checks if c.get("required"))


# --------------------------------------------------------------------------- #
# Import execution seam (injectable; default never touches production).
# --------------------------------------------------------------------------- #
def execute_import(
    *,
    mode: str,
    production_video_uuid: Any,
    source_version: str,
    row_count: int,
) -> dict[str, Any]:
    """Legacy dry-run summary builder (no DB, no mutation).

    Stage B5 wires the real, DB-backed guarded import in the route via
    ``import_execution.perform_guarded_import``; this helper remains only to
    describe a dry-run's would-write counts without touching any table.
    """
    return {
        "status": "completed",
        "mode": "dry_run",
        "video_id": str(production_video_uuid) if production_video_uuid is not None else None,
        "source_version": source_version,
        "inserted": row_count,
        "updated": 0,
        "removed": 0,
        "observations_written": row_count,
        "model_run_id": None,
    }


# The route calls this name; tests monkeypatch it to assert orchestration without
# any real execution.
ImportExecutor = Callable[..., dict[str, Any]]
