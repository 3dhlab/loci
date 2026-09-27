from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from app.core.config import settings


PUBLIC_PLAYBACK_MAX_WIDTH = 1920
PUBLIC_PLAYBACK_MAX_HEIGHT = 1080
PUBLIC_PLAYBACK_MAX_VIDEO_BITRATE = "3500k"
PUBLIC_PLAYBACK_BUFFER_SIZE = "7000k"
PUBLIC_PLAYBACK_KEYFRAME_INTERVAL_SECONDS = 2
PUBLIC_PLAYBACK_MAX_FRAME_RATE = 30
PUBLIC_PLAYBACK_MIN_FFMPEG_VERSION = "5.1"
PUBLIC_PLAYBACK_RECIPE_REVISION = "h264-1080p30-sdr-bt709-v2"
PUBLIC_PLAYBACK_TRANSCODE_TIMEOUT_SECONDS = 50 * 60
PUBLIC_PLAYBACK_PROCESS_STOP_GRACE_SECONDS = 5
PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT = 200
PUBLIC_PLAYBACK_DIAGNOSTIC_TAIL_BYTES = 16 * 1024

_BT2020_MATRICES = frozenset({"bt2020nc", "bt2020ncl"})
_BT2020_SDR_TRANSFERS = frozenset({"bt709", "bt2020-10", "bt2020-12"})
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_KNOWN_RANGES = frozenset({"tv", "pc"})


class MediaReviewRequired(RuntimeError):
    """The source needs a human media decision before public normalization."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class MediaProbeError(RuntimeError):
    """The source or output could not be probed safely."""


class MediaValidationError(RuntimeError):
    """A completed transcode does not satisfy the public playback contract."""


@dataclass(frozen=True)
class MediaProbe:
    video_codec: str
    video_profile: str | None
    video_level: int | None
    width: int
    height: int
    pixel_format: str | None
    color_range: str | None
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None
    r_frame_rate: Fraction | None
    average_frame_rate: Fraction | None
    duration_seconds: float
    size_bytes: int
    audio_codec: str | None
    audio_profile: str | None
    audio_sample_rate: int | None
    audio_channels: int | None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def maximum_reported_frame_rate(self) -> Fraction | None:
        rates = [rate for rate in (self.r_frame_rate, self.average_frame_rate) if rate and rate > 0]
        return max(rates) if rates else None


@dataclass(frozen=True)
class TranscodeProcessResult:
    return_code: int
    progress_lines: tuple[str, ...]
    stderr_tail: str


@dataclass(frozen=True)
class PlaybackArtifactValidation:
    recipe_revision: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    frame_rate: str
    duration_seconds: float
    keyframe_count: int
    maximum_keyframe_gap_seconds: float
    color_policy: str


@lru_cache(maxsize=1)
def require_public_playback_ffmpeg_runtime() -> str:
    """Fail before encoding when the installed FFmpeg cannot run this recipe."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError("ffmpeg_runtime_unavailable") from exc

    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    match = re.search(r"\bffmpeg version (?:\d+:)?(\d+)\.(\d+)", first_line)
    if match is None:
        raise MediaProbeError("ffmpeg_runtime_version_unknown")
    installed = (int(match.group(1)), int(match.group(2)))
    minimum = tuple(int(part) for part in PUBLIC_PLAYBACK_MIN_FFMPEG_VERSION.split("."))
    if installed < minimum:
        raise MediaProbeError("ffmpeg_runtime_too_old")
    return f"{installed[0]}.{installed[1]}"


def _fraction_or_none(value: object) -> Fraction | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalized_color_value(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return None if normalized in {"", "unknown", "unspecified", "reserved"} else normalized


def probe_media(path: str | Path, *, timeout_seconds: float = 30) -> MediaProbe:
    """Probe one local representation without deriving policy from its filename."""
    media_path = Path(path)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "stream=codec_type,codec_name,profile,level,width,height,pix_fmt,color_range,"
            "color_space,color_transfer,color_primaries,r_frame_rate,avg_frame_rate,"
            "duration,sample_rate,channels:format=duration,size"
        ),
        "-of",
        "json",
        str(media_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_seconds,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise MediaProbeError("media_probe_failed") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise MediaProbeError("media_probe_missing_streams")

    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    if video_stream is None:
        raise MediaProbeError("media_probe_missing_video")

    width = _positive_int(video_stream.get("width"))
    height = _positive_int(video_stream.get("height"))
    if not width or not height:
        raise MediaProbeError("media_probe_invalid_dimensions")

    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_seconds = _positive_float(format_payload.get("duration"))
    if duration_seconds is None:
        duration_seconds = _positive_float(video_stream.get("duration"))
    if duration_seconds is None or duration_seconds <= 0:
        raise MediaProbeError("media_probe_invalid_duration")

    size_bytes = _positive_int(format_payload.get("size"))
    if size_bytes is None:
        try:
            size_bytes = media_path.stat().st_size
        except OSError as exc:
            raise MediaProbeError("media_probe_missing_size") from exc

    return MediaProbe(
        video_codec=str(video_stream.get("codec_name") or "").lower(),
        video_profile=str(video_stream.get("profile")) if video_stream.get("profile") else None,
        video_level=_positive_int(video_stream.get("level")),
        width=width,
        height=height,
        pixel_format=str(video_stream.get("pix_fmt")) if video_stream.get("pix_fmt") else None,
        color_range=_normalized_color_value(video_stream.get("color_range")),
        color_space=_normalized_color_value(video_stream.get("color_space")),
        color_transfer=_normalized_color_value(video_stream.get("color_transfer")),
        color_primaries=_normalized_color_value(video_stream.get("color_primaries")),
        r_frame_rate=_fraction_or_none(video_stream.get("r_frame_rate")),
        average_frame_rate=_fraction_or_none(video_stream.get("avg_frame_rate")),
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
        audio_codec=(str(audio_stream.get("codec_name") or "").lower() if audio_stream else None),
        audio_profile=(str(audio_stream.get("profile")) if audio_stream and audio_stream.get("profile") else None),
        audio_sample_rate=(_positive_int(audio_stream.get("sample_rate")) if audio_stream else None),
        audio_channels=(_positive_int(audio_stream.get("channels")) if audio_stream else None),
    )


def classify_public_playback_color(probe: MediaProbe) -> str:
    """Return a closed color policy or require review before any encode starts."""
    color_values = (probe.color_range, probe.color_space, probe.color_transfer, probe.color_primaries)
    if any(value is None for value in color_values):
        raise MediaReviewRequired("missing_color_metadata")
    if probe.color_range not in _KNOWN_RANGES:
        raise MediaReviewRequired("unsupported_color_range")

    if probe.color_transfer in _HDR_TRANSFERS:
        if probe.color_primaries != "bt2020" or probe.color_space not in _BT2020_MATRICES:
            raise MediaReviewRequired("ambiguous_hdr_metadata")
        # FFmpeg's documented software tone-map path requires linear-light conversion.
        # The deployed/local build has `tonemap` but lacks both `zscale` and
        # `libplacebo`, so a trustworthy PQ/HLG -> SDR transform is unavailable.
        raise MediaReviewRequired("hdr_tonemap_toolchain_unavailable")

    if (
        probe.color_primaries == "bt709"
        and probe.color_transfer == "bt709"
        and probe.color_space == "bt709"
    ):
        return "sdr-bt709-preserve" if probe.color_range == "tv" else "sdr-bt709-full-to-limited"

    if (
        probe.color_primaries == "bt2020"
        and probe.color_transfer in _BT2020_SDR_TRANSFERS
        and probe.color_space in _BT2020_MATRICES
    ):
        return "sdr-bt2020-to-bt709"

    raise MediaReviewRequired("ambiguous_or_unsupported_color_metadata")


def public_playback_scale_filter(probe: MediaProbe | None = None) -> str:
    """Keep playback inside 1080p without increasing source dimensions or cadence."""
    filters = [
        (
            f"scale=w='min({PUBLIC_PLAYBACK_MAX_WIDTH},iw)':"
            f"h='min({PUBLIC_PLAYBACK_MAX_HEIGHT},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    ]
    if probe is not None:
        source_rate = probe.maximum_reported_frame_rate
        if source_rate is None:
            raise MediaReviewRequired("missing_frame_rate_metadata")
        if source_rate > PUBLIC_PLAYBACK_MAX_FRAME_RATE:
            filters.append(f"fps={PUBLIC_PLAYBACK_MAX_FRAME_RATE}")
    return ",".join(filters)


def public_playback_video_filter(probe: MediaProbe) -> tuple[str, str]:
    color_policy = classify_public_playback_color(probe)
    filters = [public_playback_scale_filter(probe)]
    if color_policy == "sdr-bt709-preserve":
        filters.append(
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
    else:
        # `colorspace` performs transfer, primary, matrix, and range conversion. This
        # is materially different from relabeling frames with `setparams`.
        filters.append("colorspace=all=bt709:range=tv:format=yuv420p:dither=fsb")
    return ",".join(filters), color_policy


def public_playback_transcode_command(
    source_path: str | Path,
    output_path: str | Path,
    *,
    probe: MediaProbe,
) -> list[str]:
    """Build the browser-playback command from a reviewed source probe."""
    video_filter, _ = public_playback_video_filter(probe)
    command = [
        "ffmpeg",
        "-nostdin",
        "-n",
        "-v",
        "warning",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
    ]
    if probe.has_audio:
        command.extend(["-map", "0:a:0"])
    else:
        command.append("-an")
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-crf",
            "21",
            "-maxrate",
            PUBLIC_PLAYBACK_MAX_VIDEO_BITRATE,
            "-bufsize",
            PUBLIC_PLAYBACK_BUFFER_SIZE,
            "-force_key_frames",
            f"expr:gte(t,n_forced*{PUBLIC_PLAYBACK_KEYFRAME_INTERVAL_SECONDS})",
            "-sc_threshold",
            "0",
            "-x264-params",
            "nal-hrd=vbr",
            "-max_muxing_queue_size",
            "1024",
            "-movflags",
            "+faststart",
            "-fps_mode:v:0",
            "vfr",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.1",
        ]
    )
    if probe.has_audio:
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-ar",
                "48000",
            ]
        )
    command.extend(
        [
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
    )
    return command


def _terminate_then_kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=PUBLIC_PLAYBACK_PROCESS_STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.wait(timeout=PUBLIC_PLAYBACK_PROCESS_STOP_GRACE_SECONDS)


def _binary_file_tail(stream, limit: int = PUBLIC_PLAYBACK_DIAGNOSTIC_TAIL_BYTES) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - limit), os.SEEK_SET)
    return stream.read().decode("utf-8", errors="replace")


def run_transcode_process(
    command: Sequence[str],
    *,
    on_progress_line: Callable[[str], None] | None = None,
    timeout_seconds: float = PUBLIC_PLAYBACK_TRANSCODE_TIMEOUT_SECONDS,
) -> TranscodeProcessResult:
    """Run ffmpeg with a real monotonic deadline and guaranteed child cleanup."""
    progress_lines: deque[str] = deque(maxlen=PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT)
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as stderr_file:
        selector = selectors.DefaultSelector()
        process: subprocess.Popen[bytes] | None = None
        pending = bytearray()
        deadline = time.monotonic() + timeout_seconds
        try:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=False,
                bufsize=0,
            )
            if process.stdout is None:
                raise RuntimeError("ffmpeg_progress_pipe_unavailable")
            output_fd = process.stdout.fileno()
            os.set_blocking(output_fd, False)
            selector.register(output_fd, selectors.EVENT_READ)

            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(list(command), timeout_seconds)
                events = selector.select(timeout=min(0.5, remaining))
                if not events:
                    continue
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    pending.extend(chunk)
                    while b"\n" in pending:
                        raw_line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        progress_lines.append(line)
                        if on_progress_line is not None:
                            on_progress_line(line)

            if pending:
                line = bytes(pending).decode("utf-8", errors="replace").strip()
                if line:
                    progress_lines.append(line)
                    if on_progress_line is not None:
                        on_progress_line(line)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(command), timeout_seconds)
            return_code = process.wait(timeout=remaining)
        except BaseException:
            if process is not None:
                _terminate_then_kill(process)
            raise
        finally:
            selector.close()
            if process is not None and process.stdout is not None:
                process.stdout.close()

        return TranscodeProcessResult(
            return_code=return_code,
            progress_lines=tuple(progress_lines),
            stderr_tail=_binary_file_tail(stderr_file),
        )


def _mp4_has_faststart(path: Path) -> bool:
    file_size = path.stat().st_size
    offset = 0
    saw_ftyp = False
    with path.open("rb") as stream:
        while offset + 8 <= file_size:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) != 8:
                return False
            box_size = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            header_size = 8
            if box_size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    return False
                box_size = int.from_bytes(extended, "big")
                header_size = 16
            elif box_size == 0:
                box_size = file_size - offset
            if box_size < header_size or offset + box_size > file_size:
                return False
            if box_type == b"ftyp":
                saw_ftyp = True
            elif box_type == b"moov":
                return saw_ftyp
            elif box_type == b"mdat":
                return False
            offset += box_size
    return False


def _keyframe_summary(path: Path, *, timeout_seconds: float = 60) -> tuple[int, float]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time,flags",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaValidationError("keyframe_probe_failed") from exc

    timestamps: list[float] = []
    video_end = 0.0
    for raw_line in result.stdout.splitlines():
        columns = [column.strip() for column in raw_line.split(",")]
        if len(columns) < 2 or "K" not in columns[-1]:
            try:
                packet_pts = float(columns[0])
                packet_duration = float(columns[1]) if len(columns) >= 3 else 0.0
            except (IndexError, ValueError):
                continue
            video_end = max(video_end, packet_pts + max(0.0, packet_duration))
            continue
        try:
            packet_pts = float(columns[0])
            packet_duration = float(columns[1]) if len(columns) >= 3 else 0.0
        except ValueError:
            continue
        timestamps.append(packet_pts)
        video_end = max(video_end, packet_pts + max(0.0, packet_duration))
    timestamps.sort()
    if not timestamps or abs(timestamps[0]) > 0.05:
        raise MediaValidationError("keyframe_grid_missing_initial_frame")
    gaps = [current - previous for previous, current in zip(timestamps, timestamps[1:])]
    gaps.append(max(0.0, video_end - timestamps[-1]))
    return len(timestamps), max(gaps, default=0.0)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_public_playback_artifact(
    output_path: str | Path,
    *,
    source_probe: MediaProbe,
) -> PlaybackArtifactValidation:
    """Validate the exact temporary representation before atomic publication."""
    path = Path(output_path)
    output_probe = probe_media(path)
    if output_probe.video_codec != "h264":
        raise MediaValidationError("playback_codec_mismatch")
    if (output_probe.video_profile or "").lower() != "high" or output_probe.video_level != 41:
        raise MediaValidationError("playback_h264_profile_or_level_mismatch")
    if output_probe.pixel_format != "yuv420p":
        raise MediaValidationError("playback_pixel_format_mismatch")
    if output_probe.width > PUBLIC_PLAYBACK_MAX_WIDTH or output_probe.height > PUBLIC_PLAYBACK_MAX_HEIGHT:
        raise MediaValidationError("playback_dimensions_exceed_envelope")
    if output_probe.width > source_probe.width or output_probe.height > source_probe.height:
        raise MediaValidationError("playback_dimensions_upscaled")
    if output_probe.width % 2 or output_probe.height % 2:
        raise MediaValidationError("playback_dimensions_not_even")

    output_rate = output_probe.maximum_reported_frame_rate
    source_rate = source_probe.maximum_reported_frame_rate
    if output_rate is None or source_rate is None:
        raise MediaValidationError("playback_frame_rate_unknown")
    if output_rate > PUBLIC_PLAYBACK_MAX_FRAME_RATE or output_rate > source_rate:
        raise MediaValidationError("playback_frame_rate_exceeds_envelope")

    if (
        output_probe.color_range != "tv"
        or output_probe.color_space != "bt709"
        or output_probe.color_transfer != "bt709"
        or output_probe.color_primaries != "bt709"
    ):
        raise MediaValidationError("playback_color_contract_mismatch")
    if source_probe.has_audio:
        if (
            output_probe.audio_codec != "aac"
            or (output_probe.audio_profile or "").upper() != "LC"
            or output_probe.audio_sample_rate != 48_000
            or output_probe.audio_channels != 2
        ):
            raise MediaValidationError("playback_audio_contract_mismatch")
    elif output_probe.has_audio:
        raise MediaValidationError("playback_unexpected_audio")

    if abs(output_probe.duration_seconds - source_probe.duration_seconds) > 0.25:
        raise MediaValidationError("playback_duration_drift")
    if not _mp4_has_faststart(path):
        raise MediaValidationError("playback_faststart_missing")

    keyframe_count, maximum_keyframe_gap = _keyframe_summary(path)
    frame_period = float(1 / output_rate)
    if maximum_keyframe_gap > PUBLIC_PLAYBACK_KEYFRAME_INTERVAL_SECONDS + frame_period + 0.02:
        raise MediaValidationError("playback_keyframe_gap_exceeded")

    _, color_policy = public_playback_video_filter(source_probe)
    return PlaybackArtifactValidation(
        recipe_revision=PUBLIC_PLAYBACK_RECIPE_REVISION,
        sha256=_sha256_file(path),
        size_bytes=path.stat().st_size,
        width=output_probe.width,
        height=output_probe.height,
        frame_rate=str(output_rate),
        duration_seconds=output_probe.duration_seconds,
        keyframe_count=keyframe_count,
        maximum_keyframe_gap_seconds=maximum_keyframe_gap,
        color_policy=color_policy,
    )


def transcode_output_dir() -> Path:
    return Path(settings.media_root).resolve() / settings.transcode_output_subdir


def transcode_output_path(video_id: str) -> Path:
    """Return the legacy stable rendition path for an unmigrated video row."""
    return transcode_output_dir() / f"{video_id}.mp4"


def versioned_transcode_output_path(
    video_id: str,
    content_sha256: str,
    publication_token: str,
) -> Path:
    """Return a new immutable local key for one validated representation."""

    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise MediaValidationError("invalid_playback_sha256")
    if not re.fullmatch(r"[0-9a-f]{32}", publication_token):
        raise MediaValidationError("invalid_publication_token")
    return transcode_output_dir() / f"{video_id}.{content_sha256}.{publication_token}.mp4"


def video_playback_path(video) -> Path | None:
    """Resolve a persisted rendition key inside the configured media root."""

    storage_key = getattr(video, "playback_storage_key", None)
    if storage_key is None:
        return transcode_output_path(str(video.id))
    if not isinstance(storage_key, str) or Path(storage_key).name != storage_key:
        return None
    pattern = rf"{re.escape(str(video.id))}\.([0-9a-f]{{64}})\.[0-9a-f]{{32}}\.mp4"
    match = re.fullmatch(pattern, storage_key)
    persisted_digest = getattr(video, "playback_sha256_checksum", None)
    if match is None or match.group(1) != persisted_digest:
        return None
    root = transcode_output_dir()
    candidate = (root / storage_key).resolve()
    return candidate if candidate.parent == root.resolve() else None


def transcode_temp_output_path(video_id: str, attempt_token: str | None = None) -> Path:
    suffix = f".{attempt_token}" if attempt_token else ""
    return transcode_output_dir() / f"{video_id}{suffix}.tmp.mp4"
