"""Check real encoded media and the preservation contract for generated refreshes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.scripts.demo_storyboard import BACKGROUND, CHAPTERS, FPS, WHITE, render_frame
from app.scripts.generate_demo_assets import generate_assets, generate_clip
from app.scripts.replace_demo_media import validate_synthetic_assets


@pytest.fixture(scope='module')
def assets(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and ffprobe are required for the demo media integration test.')
    destination = tmp_path_factory.mktemp('demo-storyboard')
    generate_assets(destination)
    return destination


def test_storyboard_changes_inside_and_between_chapters():
    assert render_frame(0) != render_frame(FPS)
    assert len({hashlib.sha256(render_frame(i * 4 * FPS)).digest() for i in range(3)}) == 3
    assert [(c['start_ms'],c['end_ms']) for c in CHAPTERS] == [(0,4000),(4000,8000),(8000,12000)]
    assert len({c['shape'] for c in CHAPTERS}) == 3
    with pytest.raises(ValueError):
        render_frame(12 * FPS)


def test_text_and_feature_halo_have_strong_contrast():
    def luminance(color):
        normalized = [v / 255 for v in color]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in normalized]
        return sum(v * weight for v,weight in zip(linear,(.2126,.7152,.0722)))
    assert (luminance(WHITE)+.05)/(luminance(BACKGROUND)+.05) > 7
    # Every chapter's white feature is backed by a dark outline, including amber.
    for frame, feature, halo in ((0,(722,216),(722,221)),
                                 (4*FPS,(722,216),(722,221)),
                                 (8*FPS,(842,185),(842,192))):
        pixels = render_frame(frame)
        for point, color in ((feature,WHITE),(halo,BACKGROUND)):
            offset = (point[1]*960+point[0])*3
            assert pixels[offset:offset+3] == bytes(color)


def test_real_video_and_clip_boundaries(assets, tmp_path):
    assert validate_synthetic_assets(assets) == assets / 'colors.mp4'
    probe = subprocess.run(['ffprobe','-v','error','-show_entries','stream=nb_frames,pix_fmt',
                            '-of','json',str(assets/'colors.mp4')],capture_output=True,text=True,check=True)
    assert json.loads(probe.stdout)['streams'] == [{'pix_fmt':'yuv420p','nb_frames':'288'}]
    for index, chapter in enumerate(CHAPTERS,1):
        clip = tmp_path / f'clip-{index}.mp4'
        generate_clip(assets/'colors.mp4',clip,chapter['start_ms'],chapter['end_ms'])
        probe = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                                '-of','json',str(clip)],capture_output=True,text=True,check=True)
        assert float(json.loads(probe.stdout)['format']['duration']) == 4
        assert clip.with_suffix('.jpg').stat().st_size > 100
        decoded = subprocess.run(['ffmpeg','-v','error','-i',str(clip),'-frames:v','1',
                                  '-f','rawvideo','-pix_fmt','rgb24','pipe:1'],capture_output=True,check=True).stdout
        expected = render_frame(chapter['start_ms'] * FPS // 1000)
        # Absolute video time is burned into the original frames. A clip starting
        # at 8s must show 00:08.0 even though its own playback clock starts at 0.
        def clock_region(pixels):
            return b''.join(pixels[(y*960+486)*3:(y*960+654)*3] for y in range(428,456))
        actual_clock, expected_clock = clock_region(decoded), clock_region(expected)
        assert len(decoded) == 960*540*3
        assert sum(abs(a-b) for a,b in zip(actual_clock,expected_clock))/len(actual_clock) < 8
    subprocess.run(['ffmpeg','-v','error','-i',str(assets/'colors.mp4'),'-f','null','-'],check=True)


def test_refresh_rejects_modified_media_or_timeline(assets, tmp_path):
    shutil.copyfile(assets/'colors.mp4',tmp_path/'colors.mp4')
    manifest = json.loads((assets/'demo-manifest.json').read_text())
    manifest['chapters'][2]['start_ms'] = 7000
    (tmp_path/'demo-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(SystemExit,match='chapter manifest'):
        validate_synthetic_assets(tmp_path)
    shutil.copyfile(assets/'demo-manifest.json',tmp_path/'demo-manifest.json')
    with (tmp_path/'colors.mp4').open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(SystemExit,match='checksum'):
        validate_synthetic_assets(tmp_path)
