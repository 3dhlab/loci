"""Generate dormant public-video candidates with bounded, resumable execution.

The command never changes application rows or selected playback.  It reads a
reviewed JSON manifest, writes immutable candidates to a separate directory,
and records a privacy-safe result manifest after every item.  Execution is
deliberately serialized: the shared production host policy allows one encoder,
and operators can run the same manifest again to reuse validated outputs.

Example::

    python -m app.scripts.generate_public_video_candidates \
      --manifest release-evidence/.../VIDEO-CANDIDATE-INPUT-2026-08.json \
      --source-root /secure/media \
      --output-root /secure/staging/candidates \
      --execute
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.services.media_transcode import (
    PUBLIC_PLAYBACK_RECIPE_REVISION,
    MediaValidationError,
    PlaybackArtifactValidation,
    probe_media,
    public_playback_transcode_command,
    require_public_playback_ffmpeg_runtime,
    run_transcode_process,
    validate_public_playback_artifact,
)


MANIFEST_VERSION = 1
MAX_ITEMS = 100
MIN_FREE_BYTES = 3 * 1024**3
FULL_DECODE_TIMEOUT_SECONDS = 75 * 60
OBJECTIVE_SAMPLE_SECONDS = 8


class CandidateBatchError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_input_manifest(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateBatchError("input_manifest_invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise CandidateBatchError("input_manifest_version_invalid")
    rows = payload.get("videos")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ITEMS:
        raise CandidateBatchError("input_manifest_item_count_invalid")
    required = {"public_object_id", "video_id", "source_filename", "source_sha256"}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise CandidateBatchError("input_manifest_item_invalid")
        video_id = str(row["video_id"])
        if video_id in seen:
            raise CandidateBatchError("input_manifest_duplicate_video")
        seen.add(video_id)
        source_name = str(row["source_filename"])
        if Path(source_name).name != source_name:
            raise CandidateBatchError("input_manifest_source_name_invalid")
        digest = str(row["source_sha256"])
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise CandidateBatchError("input_manifest_source_sha256_invalid")
    return rows


def _confined_source(root: Path, filename: str) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / filename).resolve(strict=True)
    if candidate.parent != resolved_root or not candidate.is_file() or candidate.is_symlink():
        raise CandidateBatchError("source_file_unavailable")
    return candidate


@contextmanager
def _video_lock(output_root: Path, video_id: str) -> Iterator[bool]:
    lock_root = output_root / ".locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{video_id}.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_checked(command: list[str], *, timeout: float, failure_code: str) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateBatchError(failure_code) from exc
    if result.returncode != 0:
        raise CandidateBatchError(failure_code)
    return result.stderr


def _validate_complete_decode(path: Path) -> None:
    _run_checked(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ],
        timeout=FULL_DECODE_TIMEOUT_SECONDS,
        failure_code="complete_decode_failed",
    )


def _validate_ending(path: Path) -> None:
    diagnostics = _run_checked(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "info",
            "-sseof",
            "-10",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=2:pix_th=0.02,freezedetect=n=0.003:d=2",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=120,
        failure_code="ending_decode_failed",
    )
    if "black_duration:" in diagnostics or "freeze_duration:" in diagnostics:
        raise CandidateBatchError("ending_review_required")


def _stream_durations(path: Path) -> tuple[float | None, float | None]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise CandidateBatchError("stream_duration_probe_failed") from exc
    durations: dict[str, float] = {}
    for stream in payload.get("streams", []):
        codec_type = stream.get("codec_type")
        try:
            duration = float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
        durations.setdefault(codec_type, duration)
    return durations.get("video"), durations.get("audio")


def _validate_av_sync(path: Path) -> float | None:
    video_duration, audio_duration = _stream_durations(path)
    if audio_duration is None:
        return None
    if video_duration is None:
        raise CandidateBatchError("video_duration_missing")
    delta = abs(video_duration - audio_duration)
    if delta > 0.25:
        raise CandidateBatchError("av_duration_drift")
    return round(delta, 6)


def _vmaf_window(source: Path, candidate: Path, start_seconds: float) -> float:
    duration = OBJECTIVE_SAMPLE_SECONDS
    candidate_probe = probe_media(candidate)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "info",
                "-ss",
                f"{max(0.0, start_seconds):.3f}",
                "-t",
                str(duration),
                "-i",
                str(source),
                "-ss",
                f"{max(0.0, start_seconds):.3f}",
                "-t",
                str(duration),
                "-i",
                str(candidate),
                "-filter_complex",
                (
                    f"[0:v]scale={candidate_probe.width}:{candidate_probe.height}:"
                    "force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2,setpts=PTS-STARTPTS[reference];"
                    "[1:v]setpts=PTS-STARTPTS[distorted];"
                    "[distorted][reference]libvmaf=n_threads=2"
                ),
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateBatchError("objective_quality_failed") from exc
    if result.returncode != 0:
        raise CandidateBatchError("objective_quality_failed")
    marker = "VMAF score:"
    score_lines = [line for line in result.stderr.splitlines() if marker in line]
    if not score_lines:
        raise CandidateBatchError("objective_quality_score_missing")
    try:
        return float(score_lines[-1].split(marker, 1)[1].strip())
    except ValueError as exc:
        raise CandidateBatchError("objective_quality_score_invalid") from exc


def _representative_vmaf(source: Path, candidate: Path, duration_seconds: float) -> dict:
    maximum_start = max(0.0, duration_seconds - OBJECTIVE_SAMPLE_SECONDS)
    starts = sorted({0.0, maximum_start / 2, maximum_start})
    scores = [_vmaf_window(source, candidate, start) for start in starts]
    return {
        "sample_seconds": OBJECTIVE_SAMPLE_SECONDS,
        "window_starts_seconds": [round(value, 3) for value in starts],
        "scores": [round(value, 3) for value in scores],
        "mean": round(sum(scores) / len(scores), 3),
        "minimum": round(min(scores), 3),
    }


def _artifact_payload(
    artifact: PlaybackArtifactValidation,
    *,
    filename: str,
    av_duration_delta_seconds: float | None,
    objective_quality: dict,
) -> dict:
    payload = asdict(artifact)
    payload.update(
        {
            "filename": filename,
            "complete_decode": "passed",
            "ending_integrity": "passed",
            "av_duration_delta_seconds": av_duration_delta_seconds,
            "objective_quality": objective_quality,
        }
    )
    return payload


def _existing_candidate(output_root: Path, video_id: str) -> Path | None:
    matches = sorted(output_root.glob(f"{video_id}.[0-9a-f]" + "[0-9a-f]" * 63 + ".mp4"))
    return matches[0] if len(matches) == 1 else None


def _free_space_preflight(output_root: Path, minimum_free_bytes: int) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output_root).free < minimum_free_bytes:
        raise CandidateBatchError("output_storage_preflight_failed")


def _generate_one(row: dict, source_root: Path, output_root: Path) -> dict:
    video_id = str(row["video_id"])
    public_object_id = str(row["public_object_id"])
    source = _confined_source(source_root, str(row["source_filename"]))
    if _sha256_file(source) != row["source_sha256"]:
        raise CandidateBatchError("source_identity_mismatch")
    source_probe = probe_media(source)
    existing = _existing_candidate(output_root, video_id)
    if existing is not None:
        artifact = validate_public_playback_artifact(existing, source_probe=source_probe)
        if artifact.sha256 not in existing.name:
            raise CandidateBatchError("existing_candidate_identity_mismatch")
        _validate_complete_decode(existing)
        _validate_ending(existing)
        av_delta = _validate_av_sync(existing)
        objective = _representative_vmaf(source, existing, source_probe.duration_seconds)
        return {
            "public_object_id": public_object_id,
            "video_id": video_id,
            "status": "reused",
            "source_sha256": row["source_sha256"],
            "artifact": _artifact_payload(
                artifact,
                filename=existing.name,
                av_duration_delta_seconds=av_delta,
                objective_quality=objective,
            ),
        }

    temporary = output_root / f".{video_id}.{os.getpid()}.candidate.tmp.mp4"
    if temporary.exists():
        raise CandidateBatchError("temporary_output_exists")
    try:
        command = public_playback_transcode_command(source, temporary, probe=source_probe)
        process = run_transcode_process(command)
        if process.return_code != 0:
            raise CandidateBatchError("encode_failed")
        artifact = validate_public_playback_artifact(temporary, source_probe=source_probe)
        _validate_complete_decode(temporary)
        _validate_ending(temporary)
        av_delta = _validate_av_sync(temporary)
        objective = _representative_vmaf(source, temporary, source_probe.duration_seconds)
        final_path = output_root / f"{video_id}.{artifact.sha256}.mp4"
        if final_path.exists():
            if _sha256_file(final_path) != artifact.sha256:
                raise CandidateBatchError("immutable_destination_conflict")
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(final_path)
        return {
            "public_object_id": public_object_id,
            "video_id": video_id,
            "status": "completed",
            "source_sha256": row["source_sha256"],
            "artifact": _artifact_payload(
                artifact,
                filename=final_path.name,
                av_duration_delta_seconds=av_delta,
                objective_quality=objective,
            ),
        }
    finally:
        temporary.unlink(missing_ok=True)


def run_batch(args: argparse.Namespace) -> int:
    rows = _read_input_manifest(args.manifest)
    selected = set(args.retry or [])
    if selected:
        rows = [row for row in rows if row["video_id"] in selected]
        if len(rows) != len(selected):
            raise CandidateBatchError("retry_video_unknown")
    _free_space_preflight(args.output_root, args.minimum_free_bytes)
    runtime = require_public_playback_ffmpeg_runtime()
    result_path = args.output_root / "VIDEO-CANDIDATE-RESULTS.json"
    results: dict[str, dict] = {}
    if result_path.exists():
        try:
            prior = json.loads(result_path.read_text(encoding="utf-8"))
            results = {
                str(item["video_id"]): item
                for item in prior.get("videos", [])
                if isinstance(item, dict) and item.get("video_id")
            }
        except (OSError, json.JSONDecodeError):
            raise CandidateBatchError("result_manifest_invalid")

    def persist() -> None:
        _atomic_json(
            result_path,
            {
                "version": MANIFEST_VERSION,
                "recipe_revision": PUBLIC_PLAYBACK_RECIPE_REVISION,
                "ffmpeg_runtime": runtime,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mode": "execute" if args.execute else "dry-run",
                "workers": 1,
                "videos": [results[key] for key in sorted(results)],
            },
        )

    failures = 0
    for row in rows:
        video_id = str(row["video_id"])
        if not args.execute:
            results[video_id] = {
                "public_object_id": str(row["public_object_id"]),
                "video_id": video_id,
                "status": "planned",
                "source_sha256": row["source_sha256"],
            }
            persist()
            continue
        with _video_lock(args.output_root, video_id) as acquired:
            if not acquired:
                results[video_id] = {
                    "public_object_id": str(row["public_object_id"]),
                    "video_id": video_id,
                    "status": "already_running",
                    "source_sha256": row["source_sha256"],
                }
                failures += 1
                persist()
                continue
            try:
                results[video_id] = _generate_one(row, args.source_root, args.output_root)
            except Exception as exc:
                failures += 1
                reason = str(exc) if isinstance(exc, (CandidateBatchError, MediaValidationError)) else "unexpected_error"
                results[video_id] = {
                    "public_object_id": str(row["public_object_id"]),
                    "video_id": video_id,
                    "status": "failed",
                    "source_sha256": row["source_sha256"],
                    "reason": reason,
                }
            persist()
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate dormant public-video candidates safely.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Encode candidates. The default is dry-run.")
    parser.add_argument("--retry", action="append", help="Retry one manifest video UUID; repeat as needed.")
    parser.add_argument("--minimum-free-bytes", type=int, default=MIN_FREE_BYTES)
    return parser


def main() -> int:
    try:
        return run_batch(build_parser().parse_args())
    except CandidateBatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
