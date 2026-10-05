from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import videos
from app.models.entities import VideoStatus


class Session:
    def __init__(self, video):
        self.video = video

    def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.video)


def test_authoring_stream_uses_the_current_persisted_rendition(tmp_path, monkeypatch):
    media = tmp_path / 'current.mp4'
    media.write_bytes(b'current approved rendition')
    video = SimpleNamespace(id=uuid4(), status=VideoStatus.READY, original_filename='cube.mp4')
    monkeypatch.setattr(videos, 'video_playback_path', lambda value: media if value is video else None)
    monkeypatch.setattr(videos, 'transcode_output_path', lambda value: tmp_path / 'old.mp4')
    response = videos.stream_video(video.id, Session(video), SimpleNamespace(id=uuid4()))
    assert response.path == media


def test_authoring_stream_rejects_invalid_persisted_identity(monkeypatch):
    video = SimpleNamespace(id=uuid4(), status=VideoStatus.READY)
    monkeypatch.setattr(videos, 'video_playback_path', lambda value: None)
    with pytest.raises(HTTPException) as error:
        videos.stream_video(video.id, Session(video), SimpleNamespace(id=uuid4()))
    assert error.value.status_code == 404
