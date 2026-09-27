from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.core.config import settings


def visual_window_root_dir() -> Path:
    return Path(settings.media_root).resolve() / settings.visual_window_frames_subdir


def visual_window_transcript_dir(transcript_id: str | UUID) -> Path:
    return visual_window_root_dir() / str(transcript_id)


def visual_window_output_dir(transcript_id: str | UUID, window_index: int) -> Path:
    return visual_window_transcript_dir(transcript_id) / f"window-{int(window_index):04d}"


def visual_window_sample_path(transcript_id: str | UUID, window_index: int, sample_index: int) -> Path:
    return visual_window_output_dir(transcript_id, window_index) / f"sample-{int(sample_index):02d}.jpg"


def visual_window_thumbnail_path(transcript_id: str | UUID, window_index: int) -> Path:
    return visual_window_output_dir(transcript_id, window_index) / "thumbnail.jpg"