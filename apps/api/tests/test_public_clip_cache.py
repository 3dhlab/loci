from types import SimpleNamespace

import pytest

from app.api.public.endpoints import clips
from app.core.config import settings


@pytest.mark.parametrize('environment', ['development', 'staging', 'production'])
def test_unversioned_clip_media_stays_uncached_after_replacement(tmp_path, monkeypatch, environment):
    monkeypatch.setattr(settings, 'semantic_env', environment)
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'clip')
    video.with_suffix('.jpg').write_bytes(b'poster')
    monkeypatch.setattr(clips, '_public_clip_or_404', lambda db, slug: SimpleNamespace(output_mp4_path=str(video)))
    assert clips.stream_public_clip('demo-clip', None).headers['cache-control'] == 'no-store'
    assert clips.stream_public_clip_poster('demo-clip', None).headers['cache-control'] == 'no-store'
