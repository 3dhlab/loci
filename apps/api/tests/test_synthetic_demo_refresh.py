"""Isolated regressions for the synthetic demo media refresh."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.entities import (
    AnnotationReviewStatus,
    Clip,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    Segment,
    Transcript,
    User,
    Video,
)
from app.scripts import replace_demo_media as refresh
from app.scripts.demo_storyboard import CHAPTERS
from app.scripts.seed_demo import IDS, TEXT


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Small transactional stand-in; never opens the configured application DB."""

    def __init__(self, rows, commit_error=None):
        self.rows = rows
        self.commit_error = commit_error
        self.persisted = {id(row): dict(vars(row)) for group in rows.values() for row in group}
        self.commit_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            for group in self.rows.values():
                for row in group:
                    vars(row).clear()
                    vars(row).update(self.persisted[id(row)])
        return False

    def get(self, model, key):
        return next((row for row in self.rows[model] if row.id == key), None)

    def execute(self, statement):
        model = statement.column_descriptions[0]['entity']
        return _Rows(self.rows[model])

    def commit(self):
        self.commit_calls += 1
        if self.commit_error:
            raise self.commit_error
        self.persisted = {id(row): dict(vars(row)) for group in self.rows.values() for row in group}


def _row(model, **values):
    return SimpleNamespace(id=values.pop('id'), **values)


@pytest.fixture
def refresh_case(tmp_path, monkeypatch):
    assets = tmp_path / 'assets'
    assets.mkdir()
    source = assets / 'colors.mp4'
    source.write_bytes(b'generated-video')
    cube = assets / 'cube.glb'
    cube.write_bytes(b'generated-cube')
    manifest = {'cube_sha256': hashlib.sha256(cube.read_bytes()).hexdigest()}
    (assets / 'demo-manifest.json').write_text(json.dumps(manifest))

    old_video = tmp_path / 'existing-video.mp4'
    old_video.write_bytes(b'original-video')
    obj = _row(Object, id=IDS['object'], website_object_id='demo-cube', is_published=True)
    user = _row(User, id=IDS['user'], email='demo@example.org')
    model = _row(ObjectModel, id=IDS['model'], sha256_checksum=manifest['cube_sha256'], is_published=True)
    video = _row(Video, id=IDS['video'], stable_video_id='demo-colors', duration_ms=12000,
                 title=refresh.LEGACY_VIDEO_TITLE, playback_storage_key=old_video.name,
                 playback_sha256_checksum='old-digest', playback_file_size_bytes=13,
                 sha256_checksum='seed-source-digest', source_path='seed/colors.mp4',
                 original_filename='colors.mp4', is_published=True)
    transcript = _row(Transcript, id=IDS['transcript'], title=refresh.LEGACY_TRANSCRIPT_TITLE,
                      raw_text='original transcript', is_published=True)
    segments = []
    clips = []
    for index in range(1, 4):
        start, end = (index - 1) * 4000, index * 4000
        segments.append(_row(Segment, id=IDS[f'segment{index}'], transcript_id=IDS['transcript'],
                             position=index - 1, start_ms=start, end_ms=end, text=TEXT[index - 1]))
        old_clip = tmp_path / f'old-clip-{index}.mp4'
        old_clip.write_bytes(b'original-clip')
        clips.append(_row(Clip, id=IDS[f'clip{index}'], video_id=IDS['video'], start_ms=start,
                          end_ms=end, output_mp4_path=str(old_clip),
                          transcript_excerpt=f'original excerpt {index}'))
    annotations = []
    points = ((0, 0, .5), (0, .5, 0), (.5, 0, 0))
    for index, title in enumerate(('Front face', 'Top edge', 'Compare two sections'), 1):
        ranges = [(0, 4000), (8000, 12000)] if index == 3 else [((index - 1) * 4000, index * 4000)]
        playlist_clips = (1, 3) if index == 3 else (index,)
        labels = ('Front face / square', 'Compare view / circle') if index == 3 else (title,)
        playlist = [
            {'video_id': str(IDS['video']), 'clip_id': str(IDS[f'clip{clip_index}']),
             'transcript_segment_id': str(IDS[f'segment{clip_index}']),
             'start_ms': start, 'end_ms': end, 'label': label}
            for clip_index, (start, end), label in zip(playlist_clips, ranges, labels)
        ]
        annotations.append(_row(
            ObjectModelAnnotation, id=IDS[f'annotation{index}'], title=title,
            object_model_id=IDS['model'], video_id=IDS['video'],
            point_x=points[index - 1][0], point_y=points[index - 1][1], point_z=points[index - 1][2],
            start_ms=ranges[0][0], end_ms=ranges[0][1], playlist_json=playlist,
            review_status=AnnotationReviewStatus.ACTIVE, is_published=True,
        ))
    rows = {Object: [obj], User: [user], ObjectModel: [model], Video: [video],
            Transcript: [transcript], Segment: segments, Clip: clips,
            ObjectModelAnnotation: annotations}
    session = _FakeSession(rows)
    monkeypatch.setattr(refresh, 'SessionLocal', lambda: session)
    monkeypatch.setattr(refresh.settings, 'semantic_env', 'development')
    monkeypatch.setattr(refresh.settings, 'media_root', str(tmp_path / 'media'))
    monkeypatch.setattr(refresh.settings, 'transcode_output_subdir', 'transcoded')
    monkeypatch.setattr(refresh.settings, 'media_upload_max_bytes', 1_000_000)
    monkeypatch.setattr(refresh, 'validate_synthetic_assets', lambda _: source)

    def generate_clip(video_path, output, start, end):
        Path(output).write_bytes(f'{start}:{end}'.encode())
        Path(output).with_suffix('.jpg').write_bytes(b'poster')

    monkeypatch.setattr(refresh, 'generate_clip', generate_clip)
    return SimpleNamespace(assets=assets, rows=rows, session=session, video=video,
                           transcript=transcript, segments=segments, clips=clips,
                           annotations=annotations, old_video=old_video)


@pytest.mark.parametrize('legacy', [True, False], ids=['legacy-seed', 'current-seed'])
def test_refresh_updates_only_generated_title_aliases_and_preserves_contract(refresh_case, legacy):
    case = refresh_case
    if not legacy:
        case.video.title = refresh.CURRENT_VIDEO_TITLE
        case.transcript.title = refresh.CURRENT_TRANSCRIPT_TITLE
    else:
        for segment, text in zip(case.segments, (
            'The blue section introduces the cube and its front face.',
            'The amber section identifies the top edge of the cube.',
            'The purple section returns to the cube for comparison.',
        )):
            segment.text = text

    refresh.refresh_synthetic(case.assets)
    first_paths = [clip.output_mp4_path for clip in case.clips]
    first_video_key = case.video.playback_storage_key
    refresh.refresh_synthetic(case.assets)

    assert case.video.title == refresh.CURRENT_VIDEO_TITLE
    assert case.transcript.title == refresh.CURRENT_TRANSCRIPT_TITLE
    assert case.video.playback_storage_key != first_video_key
    assert all(Path(path).is_file() for path in first_paths)
    assert case.session.commit_calls == 2
    assert case.video.id == IDS['video'] and case.transcript.id == IDS['transcript']
    assert case.video.sha256_checksum == 'seed-source-digest'
    assert case.video.source_path == 'seed/colors.mp4' and case.video.original_filename == 'colors.mp4'
    assert case.video.is_published is True
    assert case.transcript.is_published is True
    assert case.rows[Object][0].is_published is True
    assert case.rows[ObjectModel][0].is_published is True
    assert [row.id for row in case.segments] == [IDS[f'segment{i}'] for i in range(1, 4)]
    assert [(row.start_ms, row.end_ms) for row in case.segments] == [(0, 4000), (4000, 8000), (8000, 12000)]
    assert [row.text for row in case.segments] == TEXT
    assert [row.id for row in case.clips] == [IDS[f'clip{i}'] for i in range(1, 4)]
    assert [(row.start_ms, row.end_ms) for row in case.clips] == [(0, 4000), (4000, 8000), (8000, 12000)]
    assert [row.id for row in case.annotations] == [IDS[f'annotation{i}'] for i in range(1, 4)]
    assert all(row.review_status == AnnotationReviewStatus.ACTIVE and row.is_published for row in case.annotations)


def test_refresh_preserves_custom_titles_independently(refresh_case):
    case = refresh_case
    case.video.title = '  My edited video title  '
    case.transcript.title = refresh.LEGACY_TRANSCRIPT_TITLE

    refresh.refresh_synthetic(case.assets)

    assert case.video.title == '  My edited video title  '
    assert case.transcript.title == refresh.CURRENT_TRANSCRIPT_TITLE

    case.video.title = refresh.LEGACY_VIDEO_TITLE
    case.transcript.title = 'My edited transcript'
    refresh.refresh_synthetic(case.assets)
    assert case.video.title == refresh.CURRENT_VIDEO_TITLE
    assert case.transcript.title == 'My edited transcript'


def test_refresh_guard_failure_creates_no_media(refresh_case):
    case = refresh_case
    case.segments[0].text = 'Personalized segment'
    media_dir = Path(refresh.settings.media_root) / refresh.settings.transcode_output_subdir

    with pytest.raises(SystemExit, match='Transcript has been personalized'):
        refresh.refresh_synthetic(case.assets)

    assert not media_dir.exists() or list(media_dir.iterdir()) == []
    assert case.session.commit_calls == 0


def test_partial_clip_generation_cleans_new_files_and_keeps_old_media(refresh_case, monkeypatch):
    case = refresh_case
    old_paths = [clip.output_mp4_path for clip in case.clips]
    old_key = case.video.playback_storage_key
    calls = 0

    def fail_after_partial_output(video_path, output, start, end):
        nonlocal calls
        calls += 1
        Path(output).write_bytes(b'partial clip')
        Path(output).with_suffix('.jpg').write_bytes(b'partial poster')
        if calls == 2:
            raise RuntimeError('clip generation failed')

    monkeypatch.setattr(refresh, 'generate_clip', fail_after_partial_output)
    media_dir = Path(refresh.settings.media_root) / refresh.settings.transcode_output_subdir
    with pytest.raises(RuntimeError, match='clip generation failed'):
        refresh.refresh_synthetic(case.assets)

    assert list(media_dir.iterdir()) == []
    assert case.video.playback_storage_key == old_key
    assert [clip.output_mp4_path for clip in case.clips] == old_paths
    assert all(Path(path).is_file() for path in old_paths)
    assert case.old_video.is_file()
    assert case.session.commit_calls == 0


def test_database_commit_failure_cleans_new_files_and_rolls_back_references(refresh_case):
    case = refresh_case
    old_paths = [clip.output_mp4_path for clip in case.clips]
    old_key = case.video.playback_storage_key
    case.session.commit_error = RuntimeError('commit failed')
    media_dir = Path(refresh.settings.media_root) / refresh.settings.transcode_output_subdir

    with pytest.raises(RuntimeError, match='commit failed'):
        refresh.refresh_synthetic(case.assets)

    assert list(media_dir.iterdir()) == []
    assert case.video.playback_storage_key == old_key
    assert case.video.title == refresh.LEGACY_VIDEO_TITLE
    assert case.transcript.title == refresh.LEGACY_TRANSCRIPT_TITLE
    assert [clip.output_mp4_path for clip in case.clips] == old_paths
    assert all(Path(path).is_file() for path in old_paths)
    assert case.old_video.is_file()
    assert case.session.commit_calls == 1
