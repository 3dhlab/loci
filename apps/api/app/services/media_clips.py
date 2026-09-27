from __future__ import annotations

from pathlib import Path

from app.core.config import settings


def clip_output_dir() -> Path:
    return Path(settings.media_root).resolve() / "clips"


def clip_output_path(clip_id: str) -> Path:
    return clip_output_dir() / f"{clip_id}.mp4"


def clip_temp_output_path(clip_id: str) -> Path:
    return clip_output_dir() / f"{clip_id}.tmp.mp4"


def clip_transcript_path(clip_id: str) -> Path:
    return clip_output_dir() / f"{clip_id}.transcript.txt"


def clip_poster_path(clip_id: str) -> Path:
    return clip_output_dir() / f"{clip_id}.jpg"


def clip_poster_path_for_video(path_value: str | Path) -> Path:
    return Path(path_value).with_suffix(".jpg")
