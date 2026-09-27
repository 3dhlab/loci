from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.media_transcode import (
    PUBLIC_PLAYBACK_BUFFER_SIZE,
    PUBLIC_PLAYBACK_KEYFRAME_INTERVAL_SECONDS,
    PUBLIC_PLAYBACK_MAX_FRAME_RATE,
    PUBLIC_PLAYBACK_MAX_HEIGHT,
    PUBLIC_PLAYBACK_MAX_VIDEO_BITRATE,
    PUBLIC_PLAYBACK_MAX_WIDTH,
    PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT,
    PUBLIC_PLAYBACK_RECIPE_REVISION,
    MediaProbe,
    MediaReviewRequired,
    PlaybackArtifactValidation,
    classify_public_playback_color,
    probe_media,
    public_playback_scale_filter,
    public_playback_transcode_command,
    public_playback_video_filter,
    require_public_playback_ffmpeg_runtime,
    run_transcode_process,
    transcode_temp_output_path,
    validate_public_playback_artifact,
)
from app.worker import jobs as worker_jobs

# Rec. ITU-T H.264 Table A-1, level 4.1.
H264_LEVEL_4_1_MAX_MACROBLOCKS_PER_SECOND = 245_760
H264_LEVEL_4_1_MAX_FRAME_MACROBLOCKS = 8_192


def _probe(**overrides) -> MediaProbe:
    values = {
        "video_codec": "hevc",
        "video_profile": "Main",
        "video_level": 153,
        "width": 3840,
        "height": 2160,
        "pixel_format": "yuv420p",
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "r_frame_rate": Fraction(24_000, 1001),
        "average_frame_rate": Fraction(24_000, 1001),
        "duration_seconds": 10.0,
        "size_bytes": 1000,
        "audio_codec": "aac",
        "audio_profile": "LC",
        "audio_sample_rate": 48_000,
        "audio_channels": 2,
    }
    values.update(overrides)
    return MediaProbe(**values)


def test_scale_filter_caps_dimensions_without_upscaling() -> None:
    video_filter = public_playback_scale_filter(_probe())

    assert PUBLIC_PLAYBACK_MAX_WIDTH == 1920
    assert PUBLIC_PLAYBACK_MAX_HEIGHT == 1080
    assert "min(1920,iw)" in video_filter
    assert "min(1080,ih)" in video_filter
    assert "force_original_aspect_ratio=decrease" in video_filter
    assert "force_divisible_by=2" in video_filter


def test_frame_rate_filter_is_added_only_above_the_ceiling() -> None:
    low_rate = _probe(r_frame_rate=Fraction(24, 1), average_frame_rate=Fraction(24, 1))
    high_rate = _probe(r_frame_rate=Fraction(60, 1), average_frame_rate=Fraction(60, 1))

    assert "fps=" not in public_playback_scale_filter(low_rate)
    assert f"fps={PUBLIC_PLAYBACK_MAX_FRAME_RATE}" in public_playback_scale_filter(high_rate)


def test_missing_frame_rate_requires_review() -> None:
    with pytest.raises(MediaReviewRequired, match="missing_frame_rate_metadata"):
        public_playback_scale_filter(_probe(r_frame_rate=None, average_frame_rate=None))


def test_frame_rate_and_dimensions_keep_level_4_1_truthful() -> None:
    command = public_playback_transcode_command(
        Path("source.mov"),
        Path("out.mp4"),
        probe=_probe(r_frame_rate=Fraction(60, 1), average_frame_rate=Fraction(60, 1)),
    )
    assert command[command.index("-level") + 1] == "4.1"

    # 1920x1080 codes as 1920x1088: 120 x 68 macroblocks.
    frame_macroblocks = (1920 // 16) * (1088 // 16)
    assert frame_macroblocks == 8_160
    assert frame_macroblocks <= H264_LEVEL_4_1_MAX_FRAME_MACROBLOCKS
    assert frame_macroblocks * PUBLIC_PLAYBACK_MAX_FRAME_RATE == 244_800
    assert frame_macroblocks * PUBLIC_PLAYBACK_MAX_FRAME_RATE <= H264_LEVEL_4_1_MAX_MACROBLOCKS_PER_SECOND
    assert frame_macroblocks * 60 > H264_LEVEL_4_1_MAX_MACROBLOCKS_PER_SECOND


def test_sdr_bt709_limited_is_preserved_truthfully() -> None:
    video_filter, color_policy = public_playback_video_filter(_probe())

    assert color_policy == "sdr-bt709-preserve"
    assert "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709" in video_filter
    assert "colorspace=all=bt709" not in video_filter


def test_sdr_bt709_full_range_is_converted_to_limited_range() -> None:
    video_filter, color_policy = public_playback_video_filter(_probe(color_range="pc"))

    assert color_policy == "sdr-bt709-full-to-limited"
    assert "colorspace=all=bt709:range=tv:format=yuv420p:dither=fsb" in video_filter


@pytest.mark.parametrize("transfer", ["bt709", "bt2020-10", "bt2020-12"])
def test_sdr_bt2020_uses_a_real_bt709_conversion(transfer: str) -> None:
    probe = _probe(
        color_space="bt2020nc",
        color_transfer=transfer,
        color_primaries="bt2020",
    )
    video_filter, color_policy = public_playback_video_filter(probe)

    assert color_policy == "sdr-bt2020-to-bt709"
    assert "colorspace=all=bt709:range=tv:format=yuv420p:dither=fsb" in video_filter
    assert "setparams=color_primaries=bt709" not in video_filter


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_hdr_requires_review_when_the_linear_tonemap_toolchain_is_unavailable(transfer: str) -> None:
    probe = _probe(
        pixel_format="yuv420p10le",
        color_space="bt2020nc",
        color_transfer=transfer,
        color_primaries="bt2020",
    )
    with pytest.raises(MediaReviewRequired, match="hdr_tonemap_toolchain_unavailable"):
        classify_public_playback_color(probe)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"color_primaries": None}, "missing_color_metadata"),
        ({"color_transfer": None}, "missing_color_metadata"),
        ({"color_range": None}, "missing_color_metadata"),
        ({"color_space": "bt2020nc"}, "ambiguous_or_unsupported_color_metadata"),
        (
            {
                "color_space": "bt709",
                "color_transfer": "smpte2084",
                "color_primaries": "bt709",
            },
            "ambiguous_hdr_metadata",
        ),
    ],
)
def test_missing_or_ambiguous_color_metadata_requires_review(overrides: dict, reason: str) -> None:
    with pytest.raises(MediaReviewRequired, match=reason):
        classify_public_playback_color(_probe(**overrides))


def test_command_keeps_browser_compatibility_and_measured_quality_settings() -> None:
    command = public_playback_transcode_command(
        Path("source.mov"),
        Path("normalized.mp4"),
        probe=_probe(),
    )

    assert command[:7] == ["ffmpeg", "-nostdin", "-n", "-v", "warning", "-i", "source.mov"]
    assert command[-1] == "normalized.mp4"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "faster"
    assert command[command.index("-crf") + 1] == "21"
    assert command[command.index("-maxrate") + 1] == PUBLIC_PLAYBACK_MAX_VIDEO_BITRATE
    assert command[command.index("-bufsize") + 1] == PUBLIC_PLAYBACK_BUFFER_SIZE
    assert command[command.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"
    assert command[command.index("-sc_threshold") + 1] == "0"
    assert command[command.index("-x264-params") + 1] == "nal-hrd=vbr"
    assert command[command.index("-max_muxing_queue_size") + 1] == "1024"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[command.index("-map_chapters") + 1] == "-1"
    assert PUBLIC_PLAYBACK_KEYFRAME_INTERVAL_SECONDS == 2
    assert PUBLIC_PLAYBACK_RECIPE_REVISION == "h264-1080p30-sdr-bt709-v2"


def test_command_maps_one_video_and_one_probed_audio_stream() -> None:
    command = public_playback_transcode_command(Path("source.mov"), Path("out.mp4"), probe=_probe())
    map_values = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
    assert map_values == ["0:v:0", "0:a:0"]


def test_command_keeps_a_silent_source_silent() -> None:
    command = public_playback_transcode_command(
        Path("silent-source.mov"),
        Path("out.mp4"),
        probe=_probe(
            audio_codec=None,
            audio_profile=None,
            audio_sample_rate=None,
            audio_channels=None,
        ),
    )

    assert "-an" in command
    assert "-c:a" not in command
    map_values = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
    assert map_values == ["0:v:0"]


def test_command_keeps_paths_as_separate_argv_entries() -> None:
    command = public_playback_transcode_command(
        Path("/srv/media/source; rm -rf /.mov"),
        Path("/srv/media/out.mp4"),
        probe=_probe(),
    )
    assert command[command.index("-i") + 1] == "/srv/media/source; rm -rf /.mov"
    assert command[-1] == "/srv/media/out.mp4"
    assert all(isinstance(argument, str) for argument in command)


def test_attempt_tokens_make_temporary_outputs_collision_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.media_transcode.transcode_output_dir", lambda: tmp_path)

    first = transcode_temp_output_path("video-id", "attempt-a")
    second = transcode_temp_output_path("video-id", "attempt-b")

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.name.endswith(".tmp.mp4")


def _artifact() -> PlaybackArtifactValidation:
    return PlaybackArtifactValidation(
        recipe_revision=PUBLIC_PLAYBACK_RECIPE_REVISION,
        sha256="a" * 64,
        size_bytes=1234,
        width=1920,
        height=1080,
        frame_rate="24000/1001",
        duration_seconds=10.0,
        keyframe_count=5,
        maximum_keyframe_gap_seconds=2.0,
        color_policy="sdr-bt709-preserve",
    )


def test_publication_switches_validated_bytes_before_the_ready_identity_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "video.old.mp4"
    published = tmp_path / "video.versioned.mp4"
    temp_output = tmp_path / "video.attempt.tmp.mp4"
    output.write_bytes(b"old-playback")
    temp_output.write_bytes(b"validated-playback")
    video = SimpleNamespace(id="video-id")
    db = SimpleNamespace(rollback=lambda: pytest.fail("rollback should not run"))
    ready_commits: list[dict] = []

    def record_ready_commit(_db, committed_video, **state) -> None:
        assert output.read_bytes() == b"old-playback"
        assert published.read_bytes() == b"validated-playback"
        assert committed_video.playback_recipe_version == PUBLIC_PLAYBACK_RECIPE_REVISION
        assert committed_video.playback_sha256_checksum == "a" * 64
        assert committed_video.playback_file_size_bytes == 1234
        assert committed_video.playback_color_policy == "sdr-bt709-preserve"
        assert committed_video.playback_storage_key == published.name
        ready_commits.append(state)

    monkeypatch.setattr(worker_jobs, "_set_video_transcode_state", record_ready_commit)
    monkeypatch.setattr(worker_jobs, "versioned_transcode_output_path", lambda *_args: published)
    result = worker_jobs._publish_validated_playback(
        db,
        video,
        artifact=_artifact(),
        temp_output_path=temp_output,
        publication_token="c" * 32,
    )

    assert result == published
    assert output.read_bytes() == b"old-playback"
    assert published.read_bytes() == b"validated-playback"
    assert not temp_output.exists()
    assert ready_commits == [
        {
            "status": worker_jobs.VideoStatus.READY,
            "progress_pct": 100,
            "stage": "ready",
        }
    ]
    assert list(tmp_path.glob("*.rollback.mp4")) == []


def test_publication_keeps_prior_and_unique_new_bytes_when_commit_result_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "video.old.mp4"
    published = tmp_path / "video.versioned.mp4"
    temp_output = tmp_path / "video.attempt.tmp.mp4"
    output.write_bytes(b"old-playback")
    temp_output.write_bytes(b"validated-playback")
    video = SimpleNamespace(id="video-id")
    rollback_calls: list[bool] = []
    db = SimpleNamespace(rollback=lambda: rollback_calls.append(True))

    def fail_ready_commit(_db, _video, **_state) -> None:
        assert output.read_bytes() == b"old-playback"
        assert published.read_bytes() == b"validated-playback"
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(worker_jobs, "_set_video_transcode_state", fail_ready_commit)
    monkeypatch.setattr(worker_jobs, "versioned_transcode_output_path", lambda *_args: published)
    with pytest.raises(RuntimeError, match="database commit failed"):
        worker_jobs._publish_validated_playback(
            db,
            video,
            artifact=_artifact(),
            temp_output_path=temp_output,
            publication_token="c" * 32,
        )

    assert rollback_calls == [True]
    assert output.read_bytes() == b"old-playback"
    assert published.read_bytes() == b"validated-playback"
    assert not temp_output.exists()
    assert list(tmp_path.glob("*.rollback.mp4")) == []


def test_failed_replacement_preserves_existing_ready_playback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mov"
    output = tmp_path / "video-id.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"existing-ready-playback")
    prior_started_at = object()
    video = SimpleNamespace(
        id="video-id",
        source_path=str(source),
        status=worker_jobs.VideoStatus.READY,
        transcode_progress_pct=100,
        transcode_stage="ready",
        transcode_started_at=prior_started_at,
        playback_recipe_version="prior-recipe",
        playback_sha256_checksum="b" * 64,
        playback_file_size_bytes=len(b"existing-ready-playback"),
        playback_color_policy="prior-policy",
        playback_storage_key=None,
    )

    class FakeDb:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0

        def add(self, _video) -> None:
            return None

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    db = FakeDb()
    monkeypatch.setattr(worker_jobs, "transcode_output_dir", lambda: tmp_path)
    monkeypatch.setattr(worker_jobs, "video_playback_path", lambda _video: output)
    monkeypatch.setattr(
        worker_jobs,
        "transcode_temp_output_path",
        lambda _video_id, _attempt: tmp_path / "attempt.tmp.mp4",
    )
    monkeypatch.setattr(worker_jobs, "require_public_playback_ffmpeg_runtime", lambda: None)
    monkeypatch.setattr(
        worker_jobs,
        "probe_media",
        lambda _path: (_ for _ in ()).throw(MediaReviewRequired("ambiguous-color")),
    )

    result = worker_jobs._transcode_video_with_lock(db, video, "video-id")

    assert result == {
        "status": "review_required",
        "video_id": "video-id",
        "reason": "ambiguous-color",
    }
    assert output.read_bytes() == b"existing-ready-playback"
    assert video.status == worker_jobs.VideoStatus.READY
    assert video.transcode_progress_pct == 100
    assert video.transcode_stage == "ready"
    assert video.transcode_started_at is prior_started_at
    assert video.playback_recipe_version == "prior-recipe"
    assert video.playback_sha256_checksum == "b" * 64
    assert video.playback_file_size_bytes == len(b"existing-ready-playback")
    assert video.playback_color_policy == "prior-policy"
    assert video.playback_storage_key is None
    assert db.rollbacks == 1


def test_versioned_playback_paths_confine_persisted_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.media_transcode.transcode_output_dir", lambda: tmp_path)
    video_id = "855e47f3-4b51-54e1-9340-1933fdfc2508"
    digest = "a" * 64
    token = "b" * 32
    path = worker_jobs.versioned_transcode_output_path(video_id, digest, token)
    video = SimpleNamespace(
        id=video_id,
        playback_storage_key=path.name,
        playback_sha256_checksum=digest,
    )

    assert path.parent == tmp_path
    assert worker_jobs.video_playback_path(video) == path
    assert worker_jobs.video_playback_path(
        SimpleNamespace(
            id=video_id,
            playback_storage_key="../private.mp4",
            playback_sha256_checksum=digest,
        )
    ) is None
    assert worker_jobs.video_playback_path(
        SimpleNamespace(
            id=video_id,
            playback_storage_key=path.name,
            playback_sha256_checksum="c" * 64,
        )
    ) is None


def test_worker_diagnostics_are_bounded_and_redact_private_paths() -> None:
    source = Path("/private/participant/source.mov")
    temp = Path("/private/playback/video.attempt.tmp.mp4")
    diagnostic = ("x" * 10_000) + f" input={source} output={temp}"

    redacted = worker_jobs._redact_transcode_diagnostics(diagnostic, source, temp)

    assert str(source) not in redacted
    assert str(temp) not in redacted
    assert len(redacted) <= 4000


def test_process_runner_drains_large_stderr_and_bounds_progress() -> None:
    script = (
        "import sys; "
        f"[print(f'frame={{i}}', flush=True) for i in range({PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT + 50})]; "
        "sys.stderr.write('diagnostic-' + ('x' * 70000))"
    )
    result = run_transcode_process([sys.executable, "-c", script], timeout_seconds=10)

    assert result.return_code == 0
    assert len(result.progress_lines) == PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT
    assert result.progress_lines[-1] == f"frame={PUBLIC_PLAYBACK_PROGRESS_LINE_LIMIT + 49}"
    assert result.stderr_tail.startswith("x") or result.stderr_tail.startswith("diagnostic-")
    assert len(result.stderr_tail.encode()) <= 16 * 1024


def test_process_runner_enforces_a_real_deadline_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    child_pids: list[int] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        child_pids.append(process.pid)
        return process

    monkeypatch.setattr("app.services.media_transcode.subprocess.Popen", recording_popen)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_transcode_process(
            [
                sys.executable,
                "-c",
                "import time; print('progress=continue', flush=True); time.sleep(60)",
            ],
            timeout_seconds=0.1,
        )
    assert time.monotonic() - started < 2
    assert len(child_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is required for the runtime compatibility check",
)
def test_installed_ffmpeg_satisfies_the_recipe_floor() -> None:
    require_public_playback_ffmpeg_runtime.cache_clear()
    assert tuple(int(part) for part in require_public_playback_ffmpeg_runtime().split(".")) >= (5, 1)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the bounded integration check",
)
def test_final_recipe_and_validator_round_trip_sdr_bt709(tmp_path: Path) -> None:
    source = tmp_path / "source-bt709.mp4"
    output = tmp_path / "playback.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    source_probe = probe_media(source)
    command = public_playback_transcode_command(source, output, probe=source_probe)
    result = run_transcode_process(command, timeout_seconds=60)

    assert result.return_code == 0, result.stderr_tail
    artifact = validate_public_playback_artifact(output, source_probe=source_probe)
    assert artifact.recipe_revision == PUBLIC_PLAYBACK_RECIPE_REVISION
    assert (artifact.width, artifact.height) == (640, 360)
    assert artifact.frame_rate == "24"
    assert artifact.color_policy == "sdr-bt709-preserve"
    assert artifact.keyframe_count == 2
    assert artifact.maximum_keyframe_gap_seconds == pytest.approx(2.0, abs=0.05)
    assert len(artifact.sha256) == 64


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the bounded integration check",
)
def test_sdr_bt2020_filter_writes_a_bt709_representation(tmp_path: Path) -> None:
    source = tmp_path / "source-bt2020.mp4"
    output = tmp_path / "playback.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=2",
            "-vf",
            "setparams=range=tv:color_primaries=bt2020:color_trc=bt2020-10:colorspace=bt2020nc",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    source_probe = probe_media(source)
    assert classify_public_playback_color(source_probe) == "sdr-bt2020-to-bt709"

    result = run_transcode_process(
        public_playback_transcode_command(source, output, probe=source_probe),
        timeout_seconds=60,
    )
    assert result.return_code == 0, result.stderr_tail

    output_probe = probe_media(output)
    assert output_probe.color_range == "tv"
    assert output_probe.color_space == "bt709"
    assert output_probe.color_transfer == "bt709"
    assert output_probe.color_primaries == "bt709"
    artifact = validate_public_playback_artifact(output, source_probe=source_probe)
    assert artifact.color_policy == "sdr-bt2020-to-bt709"
