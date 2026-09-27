from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Callable
from uuid import uuid4

from dotenv import dotenv_values

from app.core.config import settings
from app.db.session import SessionLocal
from app.scripts.export_public_projection import build_projection
from app.services.audit_events import append_audit_event
from app.services.publication_manifest_store import mark_publication_manifests_projected, persist_publication_manifests


@dataclass(slots=True)
class PublicSyncConfig:
    remote: str
    ssh_key: Path
    remote_app_dir: str
    remote_import_root: str
    remote_media_root: str
    manifest_basename: str
    media_list_basename: str
    media_bundle_basename: str = "public_media_bundle.tar.gz"
    sync_state_basename: str = "live_database_sync_state.json"


@dataclass(slots=True)
class PublicSyncResult:
    generated_at: str | None
    project_count: int
    object_count: int
    video_count: int
    transcript_count: int
    annotation_count: int
    media_file_count: int
    changed_media_file_count: int = 0
    manifest_changed: bool = True


@dataclass(slots=True)
class PublicSyncStatus:
    operation_id: str | None = None
    state: str = "IDLE"
    stage_key: str | None = None
    stage_label: str | None = None
    detail: str = "Push every currently published package to the live public site."
    progress_percent: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    generated_at: str | None = None
    project_count: int = 0
    object_count: int = 0
    video_count: int = 0
    transcript_count: int = 0
    annotation_count: int = 0
    media_file_count: int = 0
    error: str | None = None


ProgressCallback = Callable[..., None]

_SYNC_STATUS_LOCK = Lock()
_SYNC_STATUS = PublicSyncStatus()
_SYNC_THREAD: Thread | None = None


def _default_sync_actor(actor: dict | None, *, trigger: str, operation_id: str | None = None) -> dict:
    if isinstance(actor, dict):
        normalized = {str(key): value for key, value in actor.items() if value is not None}
    else:
        normalized = {}
    normalized.setdefault("type", "system")
    normalized.setdefault("trigger", trigger)
    if operation_id is not None:
        normalized.setdefault("operation_id", operation_id)
    return normalized


def _command_error(command: list[str], result: subprocess.CompletedProcess[str]) -> RuntimeError:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"Command exited with status {result.returncode}"
    return RuntimeError(f"Public sync command failed: {' '.join(command)}\n{detail}")


def _run(command: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise _command_error(command, result)
    return result


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_progress(value: int | float) -> int:
    return max(0, min(int(round(value)), 100))


def _snapshot_sync_status() -> PublicSyncStatus:
    with _SYNC_STATUS_LOCK:
        return replace(_SYNC_STATUS)


def _update_sync_status(operation_id: str | None, **updates) -> None:
    with _SYNC_STATUS_LOCK:
        if operation_id is not None and _SYNC_STATUS.operation_id != operation_id:
            return
        for key, value in updates.items():
            if key == "progress_percent" and value is not None:
                setattr(_SYNC_STATUS, key, _clamp_progress(value))
            else:
                setattr(_SYNC_STATUS, key, value)


def get_live_database_sync_status() -> PublicSyncStatus:
    return _snapshot_sync_status()


def _counts_from_manifest(manifest: dict) -> dict[str, int | str | None]:
    return {
        "generated_at": manifest.get("generated_at"),
        "project_count": len(manifest.get("projects", [])),
        "object_count": len(manifest.get("objects", [])),
        "video_count": len(manifest.get("videos", [])),
        "transcript_count": len(manifest.get("transcripts", [])),
        "annotation_count": len(manifest.get("object_model_annotations", [])),
    }


def _stable_projection_digest(manifest: dict) -> str:
    stable_payload = {key: value for key, value in manifest.items() if key != "generated_at"}
    corpus_phrases = stable_payload.get("corpus_phrases")
    if isinstance(corpus_phrases, list):
        stable_payload["corpus_phrases"] = [
            {
                key: value
                for key, value in row.items()
                if key not in {"created_at", "updated_at"}
            }
            if isinstance(row, dict)
            else row
            for row in corpus_phrases
        ]
    canonical = json.dumps(stable_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_media_inventory(media_paths: list[str]) -> dict[str, dict[str, int]]:
    media_root = Path(settings.media_root)
    inventory: dict[str, dict[str, int]] = {}
    for relpath in media_paths:
        source_path = media_root / relpath
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError(f"Published media file is missing locally: {source_path}")
        stat = source_path.stat()
        inventory[relpath] = {
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    return inventory


def _run_live_database_sync(operation_id: str, actor: dict | None) -> None:
    def report(**updates) -> None:
        _update_sync_status(operation_id, state="RUNNING", finished_at=None, error=None, **updates)

    try:
        result = sync_public_projection(
            progress_callback=report,
            actor=_default_sync_actor(actor, trigger="ops.live_database.sync.start", operation_id=operation_id),
        )
        if not result.manifest_changed and result.changed_media_file_count == 0:
            detail = "Live Database already up to date. No published changes were detected since the last successful sync."
        elif not result.manifest_changed:
            detail = (
                "Live Database sync complete. "
                f"Published records were unchanged and {result.changed_media_file_count} media file"
                f"{'' if result.changed_media_file_count == 1 else 's'} refreshed."
            )
        else:
            detail = (
                "Live Database sync complete. "
                f"{result.object_count} objects, {result.video_count} videos, "
                f"{result.annotation_count} annotations, and {result.media_file_count} media files promoted."
            )
        _update_sync_status(
            operation_id,
            state="SUCCEEDED",
            stage_key="complete",
            stage_label="Sync complete",
            detail=detail,
            progress_percent=100,
            finished_at=_utc_now_iso(),
            generated_at=result.generated_at,
            project_count=result.project_count,
            object_count=result.object_count,
            video_count=result.video_count,
            transcript_count=result.transcript_count,
            annotation_count=result.annotation_count,
            media_file_count=result.media_file_count,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - surfaced through API/tests
        current = _snapshot_sync_status()
        _update_sync_status(
            operation_id,
            state="FAILED",
            stage_key="failed",
            stage_label="Sync failed",
            detail=str(exc),
            progress_percent=current.progress_percent,
            finished_at=_utc_now_iso(),
            error=str(exc),
        )


def begin_live_database_sync(actor: dict | None = None) -> PublicSyncStatus:
    global _SYNC_STATUS, _SYNC_THREAD

    with _SYNC_STATUS_LOCK:
        if _SYNC_STATUS.state == "RUNNING":
            return replace(_SYNC_STATUS)

        operation_id = uuid4().hex
        _SYNC_STATUS = PublicSyncStatus(
            operation_id=operation_id,
            state="RUNNING",
            stage_key="preparing",
            stage_label="Preparing published package",
            detail="Collecting published records and approved media.",
            progress_percent=4,
            started_at=_utc_now_iso(),
            finished_at=None,
            error=None,
        )
        _SYNC_THREAD = Thread(
            target=_run_live_database_sync,
            args=(operation_id, _default_sync_actor(actor, trigger="ops.live_database.sync.start", operation_id=operation_id)),
            daemon=True,
        )
        _SYNC_THREAD.start()
        return replace(_SYNC_STATUS)


def _load_sync_config() -> PublicSyncConfig:
    if not settings.semantic_public_sync_env_file.strip():
        raise RuntimeError("Remote publication is disabled. Configure SEMANTIC_PUBLIC_SYNC_ENV_FILE to enable it.")
    env_file = Path(settings.semantic_public_sync_env_file).expanduser()
    if not env_file.is_file():
        raise RuntimeError(
            f"Live Database sync config was not found at {env_file}. "
            "Supply an explicit readable deployment configuration file."
        )

    values = {key: value for key, value in dotenv_values(env_file).items() if value}

    def required(key: str) -> str:
        value = str(values.get(key) or os.environ.get(key) or "").strip()
        if not value:
            raise RuntimeError(f"Live Database sync config is missing {key}.")
        return value

    ssh_key = Path(os.path.expanduser(required("SEMANTIC_PUBLIC_SYNC_SSH_KEY")))
    if not ssh_key.exists():
        raise RuntimeError(f"Live Database sync SSH key was not found at {ssh_key}.")

    return PublicSyncConfig(
        remote=required("SEMANTIC_PUBLIC_SYNC_REMOTE"),
        ssh_key=ssh_key,
        remote_app_dir=required("SEMANTIC_PUBLIC_SYNC_REMOTE_APP_DIR"),
        remote_import_root=required("SEMANTIC_PUBLIC_SYNC_REMOTE_IMPORT_ROOT"),
        remote_media_root=required("SEMANTIC_PUBLIC_SYNC_REMOTE_MEDIA_ROOT"),
        manifest_basename=required("SEMANTIC_PUBLIC_SYNC_MANIFEST_BASENAME"),
        media_list_basename=required("SEMANTIC_PUBLIC_SYNC_MEDIA_LIST_BASENAME"),
        sync_state_basename=str(values.get("SEMANTIC_PUBLIC_SYNC_STATE_BASENAME") or os.environ.get("SEMANTIC_PUBLIC_SYNC_STATE_BASENAME") or "live_database_sync_state.json").strip(),
    )


def _ssh(config: PublicSyncConfig, remote_command: str, *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "ssh",
            "-n",
            "-i",
            str(config.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            config.remote,
            remote_command,
        ],
        timeout=timeout,
    )


def _scp(config: PublicSyncConfig, local_path: Path, remote_dir: str, *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "scp",
            "-i",
            str(config.ssh_key),
            "-B",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            str(local_path),
            f"{config.remote}:{remote_dir.rstrip('/')}/",
        ],
        timeout=timeout,
    )


def _remote_sync_state_path(config: PublicSyncConfig) -> str:
    return f"{config.remote_import_root.rstrip('/')}/{config.sync_state_basename}"


def _load_remote_sync_state(config: PublicSyncConfig) -> dict:
    remote_state_path = _remote_sync_state_path(config)
    result = _ssh(
        config,
        f"if [ -f {shlex.quote(remote_state_path)} ]; then cat {shlex.quote(remote_state_path)}; fi",
    )
    payload = (result.stdout or "").strip()
    if not payload:
        return {}
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _create_media_bundle(bundle_path: Path, media_paths: list[str], progress_callback: ProgressCallback | None = None) -> None:
    media_root = Path(settings.media_root)
    total_media = len(media_paths)
    with tarfile.open(bundle_path, "w:gz") as archive:
        for index, relpath in enumerate(media_paths, start=1):
            source_path = media_root / relpath
            if not source_path.exists() or not source_path.is_file():
                raise RuntimeError(f"Published media file is missing locally: {source_path}")
            archive.add(source_path, arcname=relpath)
            if progress_callback:
                progress_callback(
                    stage_key="bundle_media",
                    stage_label="Bundling published media",
                    detail=f"Bundling media {index} of {total_media}: {Path(relpath).name}",
                    progress_percent=22 + int((index / max(total_media, 1)) * 12),
                )


def _record_public_sync_event(*, event_type: str, actor_json: dict, payload_json: dict) -> None:
    with SessionLocal() as db:
        append_audit_event(
            db,
            event_type=event_type,
            subject_type="public_sync",
            actor_json=actor_json,
            payload_json=payload_json,
        )
        db.commit()


def sync_public_projection(
    progress_callback: ProgressCallback | None = None,
    *,
    actor: dict | None = None,
    source_release_tag: str | None = None,
) -> PublicSyncResult:
    config = _load_sync_config()
    sync_actor = _default_sync_actor(actor, trigger="ops.live_database.sync")
    if progress_callback:
        progress_callback(
            stage_key="preparing",
            stage_label="Preparing published package",
            detail="Collecting published records and approved media.",
            progress_percent=8,
        )
    manifest, media_paths = build_projection()
    sorted_media_paths = sorted(set(media_paths))
    counts = _counts_from_manifest(manifest)
    local_projection_digest = _stable_projection_digest(manifest)
    local_media_inventory = _build_media_inventory(sorted_media_paths)

    if progress_callback:
        progress_callback(
            stage_key="persist_manifests",
            stage_label="Persisting package history",
            detail="Recording the current published object-package state locally before remote promotion.",
            progress_percent=24,
            media_file_count=len(sorted_media_paths),
            **counts,
        )
    with SessionLocal() as db:
        persisted_manifest_result = persist_publication_manifests(
            db,
            actor_json=sync_actor,
            generated_at=manifest.get("generated_at"),
            source_release_tag=source_release_tag,
        )

    if progress_callback:
        progress_callback(
            stage_key="prepared",
            stage_label="Published package ready",
            detail=(
                f"Prepared {counts['object_count']} objects, {counts['video_count']} videos, "
                f"and {len(sorted_media_paths)} media files for sync."
            ),
            progress_percent=18,
            media_file_count=len(sorted_media_paths),
            **counts,
        )

    manifest_changed = True
    changed_media_paths: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="semantic-public-sync-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            manifest_path = tmpdir_path / config.manifest_basename
            media_list_path = tmpdir_path / config.media_list_basename
            media_bundle_path = tmpdir_path / config.media_bundle_basename
            sync_state_path = tmpdir_path / config.sync_state_basename

            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            media_list_path.write_text(
                "\n".join(sorted_media_paths) + ("\n" if sorted_media_paths else ""),
                encoding="utf-8",
            )

            if progress_callback:
                progress_callback(
                    stage_key="prepare_remote",
                    stage_label="Preparing remote import paths",
                    detail="Ensuring the VPS import and media directories are ready.",
                    progress_percent=40,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )
            _ssh(
                config,
                "mkdir -p "
                f"{shlex.quote(config.remote_import_root)} "
                f"{shlex.quote(config.remote_media_root)}",
            )

            if progress_callback:
                progress_callback(
                    stage_key="compare_remote",
                    stage_label="Comparing against last sync",
                    detail="Checking which published changes actually need to be pushed.",
                    progress_percent=44,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )

            remote_state = _load_remote_sync_state(config)
            remote_projection_digest = str(remote_state.get("projection_digest") or "")
            remote_media_inventory = remote_state.get("media_inventory")
            if not isinstance(remote_media_inventory, dict):
                remote_media_inventory = {}
            remote_media_paths = set(remote_media_inventory.keys())
            local_media_path_set = set(sorted_media_paths)
            changed_media_paths = [
                relpath
                for relpath in sorted_media_paths
                if remote_media_inventory.get(relpath) != local_media_inventory.get(relpath)
            ]
            removed_media_paths = sorted(remote_media_paths - local_media_path_set)
            manifest_changed = local_projection_digest != remote_projection_digest

            if not manifest_changed and not changed_media_paths and not removed_media_paths:
                with SessionLocal() as db:
                    mark_publication_manifests_projected(
                        db,
                        persisted_manifest_result.current_manifest_ids,
                        projected_at=_utc_now_iso(),
                        actor_json=sync_actor,
                    )
                    append_audit_event(
                        db,
                        event_type="public_sync.noop",
                        subject_type="public_sync",
                        actor_json=sync_actor,
                        payload_json={
                            "generated_at": manifest.get("generated_at"),
                            "project_count": counts["project_count"],
                            "object_count": counts["object_count"],
                            "video_count": counts["video_count"],
                            "transcript_count": counts["transcript_count"],
                            "annotation_count": counts["annotation_count"],
                            "media_file_count": len(sorted_media_paths),
                            "changed_media_file_count": 0,
                            "manifest_changed": False,
                            "manifest_ids": persisted_manifest_result.current_manifest_ids,
                        },
                    )
                    db.commit()
                if progress_callback:
                    progress_callback(
                        stage_key="complete",
                        stage_label="Already up to date",
                        detail="No published changes were detected since the last successful Live Database sync.",
                        progress_percent=100,
                        media_file_count=len(sorted_media_paths),
                        **counts,
                    )
                return PublicSyncResult(
                    generated_at=manifest.get("generated_at"),
                    project_count=len(manifest.get("projects", [])),
                    object_count=len(manifest.get("objects", [])),
                    video_count=len(manifest.get("videos", [])),
                    transcript_count=len(manifest.get("transcripts", [])),
                    annotation_count=len(manifest.get("object_model_annotations", [])),
                    media_file_count=len(sorted_media_paths),
                    changed_media_file_count=0,
                    manifest_changed=False,
                )

            sync_state_payload = {
                "generated_at": manifest.get("generated_at"),
                "projection_digest": local_projection_digest,
                "media_inventory": local_media_inventory,
                "project_count": counts["project_count"],
                "object_count": counts["object_count"],
                "video_count": counts["video_count"],
                "transcript_count": counts["transcript_count"],
                "annotation_count": counts["annotation_count"],
                "media_file_count": len(sorted_media_paths),
            }
            sync_state_path.write_text(json.dumps(sync_state_payload, indent=2), encoding="utf-8")

            if progress_callback:
                progress_callback(
                    stage_key="upload_manifest",
                    stage_label="Uploading projection manifest",
                    detail=(
                        "Sending the published database manifest to the VPS."
                        if manifest_changed
                        else "Published records are unchanged. Skipping manifest re-upload."
                    ),
                    progress_percent=50,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )
            if manifest_changed:
                _scp(config, manifest_path, config.remote_import_root)

            needs_media_list = bool(changed_media_paths or removed_media_paths or manifest_changed)
            if needs_media_list:
                if progress_callback:
                    progress_callback(
                        stage_key="upload_media_list",
                        stage_label="Uploading media inventory",
                        detail=(
                            "Updating the approved media file list for prune and sync-state tracking."
                        ),
                        progress_percent=56,
                        media_file_count=len(sorted_media_paths),
                        **counts,
                    )
                _scp(config, media_list_path, config.remote_import_root)

            remote_bundle_path = f"{config.remote_import_root.rstrip('/')}/{config.media_bundle_basename}"
            if changed_media_paths:
                _create_media_bundle(media_bundle_path, changed_media_paths, progress_callback=progress_callback)
                if progress_callback:
                    progress_callback(
                        stage_key="upload_media_bundle",
                        stage_label="Uploading media bundle",
                        detail=f"Uploading {len(changed_media_paths)} changed media file{'' if len(changed_media_paths) == 1 else 's'} to the VPS.",
                        progress_percent=72,
                        media_file_count=len(sorted_media_paths),
                        **counts,
                    )
                _scp(config, media_bundle_path, config.remote_import_root)
                if progress_callback:
                    progress_callback(
                        stage_key="extract_media_bundle",
                        stage_label="Installing media bundle",
                        detail="Installing changed media files into the public media root.",
                        progress_percent=82,
                        media_file_count=len(sorted_media_paths),
                        **counts,
                    )
                _ssh(
                    config,
                    f"tar -xzf {shlex.quote(remote_bundle_path)} -C {shlex.quote(config.remote_media_root)} "
                    f"&& rm -f {shlex.quote(remote_bundle_path)}",
                )
            elif progress_callback:
                progress_callback(
                    stage_key="upload_media_bundle",
                    stage_label="Media upload skipped",
                    detail="No published media files changed since the last sync.",
                    progress_percent=82,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )

            remote_manifest_in_container = f"/var/lib/semantic/import/{config.manifest_basename}"
            if manifest_changed and progress_callback:
                progress_callback(
                    stage_key="import_projection",
                    stage_label="Importing published records",
                    detail="Applying the published projection inside the public API container.",
                    progress_percent=92,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )
            if manifest_changed:
                _ssh(
                    config,
                    f"cd {shlex.quote(config.remote_app_dir)} "
                    "&& docker compose --env-file .env.public -f docker-compose.public.yml "
                    "exec -T api env PYTHONPATH=/app "
                    f"python -m app.scripts.import_public_projection --manifest {shlex.quote(remote_manifest_in_container)}",
                    timeout=1200,
                )

            prune_script = "\n".join(
                [
                    "from pathlib import Path",
                    "import sys",
                    "",
                    "media_root = Path(sys.argv[1])",
                    "keep_list = Path(sys.argv[2])",
                    "keep = {line.strip() for line in keep_list.read_text(encoding='utf-8').splitlines() if line.strip()} if keep_list.exists() else set()",
                    "",
                    "for path in sorted(media_root.rglob('*'), key=lambda item: len(item.parts), reverse=True):",
                    "    if path.is_file() and path.relative_to(media_root).as_posix() not in keep:",
                    "        path.unlink()",
                    "",
                    "for path in sorted(media_root.rglob('*'), key=lambda item: len(item.parts), reverse=True):",
                    "    if path.is_dir():",
                    "        try:",
                    "            path.rmdir()",
                    "        except OSError:",
                    "            pass",
                ]
            )
            remote_media_list_path = f"{config.remote_import_root.rstrip('/')}/{config.media_list_basename}"
            if removed_media_paths and progress_callback:
                progress_callback(
                    stage_key="prune_media",
                    stage_label="Pruning orphaned media",
                    detail="Removing public media files that are no longer part of the published projection.",
                    progress_percent=97,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )
            if removed_media_paths:
                _ssh(
                    config,
                    "python3 -c "
                    f"{shlex.quote(prune_script)} "
                    f"{shlex.quote(config.remote_media_root)} "
                    f"{shlex.quote(remote_media_list_path)}",
                )

            if progress_callback:
                progress_callback(
                    stage_key="persist_sync_state",
                    stage_label="Saving sync state",
                    detail="Recording this successful sync so later runs can push only changed media.",
                    progress_percent=99,
                    media_file_count=len(sorted_media_paths),
                    **counts,
                )
            _scp(config, sync_state_path, config.remote_import_root)
    except Exception as exc:
        _record_public_sync_event(
            event_type="public_sync.failed",
            actor_json=sync_actor,
            payload_json={
                "generated_at": manifest.get("generated_at"),
                "project_count": counts["project_count"],
                "object_count": counts["object_count"],
                "video_count": counts["video_count"],
                "transcript_count": counts["transcript_count"],
                "annotation_count": counts["annotation_count"],
                "media_file_count": len(sorted_media_paths),
                "changed_media_file_count": len(changed_media_paths),
                "manifest_changed": manifest_changed,
                "manifest_ids": persisted_manifest_result.current_manifest_ids,
                "error": str(exc),
            },
        )
        raise

    with SessionLocal() as db:
        mark_publication_manifests_projected(
            db,
            persisted_manifest_result.current_manifest_ids,
            projected_at=_utc_now_iso(),
            actor_json=sync_actor,
        )
        append_audit_event(
            db,
            event_type="public_sync.completed",
            subject_type="public_sync",
            actor_json=sync_actor,
            payload_json={
                "generated_at": manifest.get("generated_at"),
                "project_count": counts["project_count"],
                "object_count": counts["object_count"],
                "video_count": counts["video_count"],
                "transcript_count": counts["transcript_count"],
                "annotation_count": counts["annotation_count"],
                "media_file_count": len(sorted_media_paths),
                "changed_media_file_count": len(changed_media_paths),
                "manifest_changed": manifest_changed,
                "manifest_ids": persisted_manifest_result.current_manifest_ids,
            },
        )
        db.commit()

    return PublicSyncResult(
        generated_at=manifest.get("generated_at"),
        project_count=len(manifest.get("projects", [])),
        object_count=len(manifest.get("objects", [])),
        video_count=len(manifest.get("videos", [])),
        transcript_count=len(manifest.get("transcripts", [])),
        annotation_count=len(manifest.get("object_model_annotations", [])),
        media_file_count=len(sorted_media_paths),
        changed_media_file_count=len(changed_media_paths),
        manifest_changed=manifest_changed,
    )
