"""Replace media in the seeded local demo and require review of linked content."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import select
from app.api.v1.endpoints.object_models import upload_object_model
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import Object, ObjectModel, ObjectModelAnnotation, AnnotationReviewStatus, Transcript, User, Video, Clip, Segment
from app.scripts.seed_demo import IDS, TEXT
from app.scripts.generate_demo_assets import generate_clip
from app.scripts.demo_storyboard import CHAPTERS, FPS, HEIGHT, VERSION, WIDTH

LEGACY_VIDEO_TITLE = 'Three color sections'
CURRENT_VIDEO_TITLE = 'Three cube views'
LEGACY_TRANSCRIPT_TITLE = 'Color demonstration transcript'
CURRENT_TRANSCRIPT_TITLE = 'Cube view demonstration transcript'


def validate_synthetic_assets(assets: Path) -> Path:
    """Require the exact generated storyboard contract before preserving review."""
    video = assets / 'colors.mp4'
    manifest = json.loads((assets / 'demo-manifest.json').read_text())
    expected = {'version': VERSION, 'width': WIDTH, 'height': HEIGHT, 'fps': FPS,
                'duration_ms': 12000, 'chapters': list(CHAPTERS)}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise SystemExit('Synthetic refresh requires the current generated chapter manifest.')
    if not video.is_file() or video.stat().st_size > settings.media_upload_max_bytes:
        raise SystemExit('Synthetic video must be a local file within the media upload limit.')
    with video.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != manifest.get('video_sha256'):
        raise SystemExit('Synthetic video checksum differs from its generated manifest.')
    probe = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                            '-show_entries','stream=width,height,r_frame_rate,codec_name',
                            '-of','json',str(video)],capture_output=True,text=True,check=True)
    info = json.loads(probe.stdout)
    streams = info['streams']
    if (len(streams) != 1 or streams[0] != {'codec_name': 'h264', 'width': WIDTH,
            'height': HEIGHT, 'r_frame_rate': f'{FPS}/1'}
            or abs(float(info['format']['duration']) - 12) > .001):
        raise SystemExit('Synthetic video must be the silent 12-second generated H.264 storyboard.')
    return video


def refresh_synthetic(assets: Path) -> None:
    """Refresh an untouched seed while retaining its annotation/review contract.

    Ordinary --video replacement continues to withdraw linked publication.
    This opt-in path accepts the generated silent sample and original seed rows.
    """
    if settings.semantic_env.lower() != 'development':
        raise SystemExit('Synthetic refresh requires SEMANTIC_ENV=development.')
    source = validate_synthetic_assets(assets)
    cube_digest = hashlib.sha256((assets / 'cube.glb').read_bytes()).hexdigest()
    if cube_digest != json.loads((assets / 'demo-manifest.json').read_text()).get('cube_sha256'):
        raise SystemExit('Generated cube checksum differs from its manifest.')
    legacy_text = ['The blue section introduces the cube and its front face.',
                   'The amber section identifies the top edge of the cube.',
                   'The purple section returns to the cube for comparison.']
    with SessionLocal() as db:
        obj, user = db.get(Object, IDS['object']), db.get(User, IDS['user'])
        model, video = db.get(ObjectModel, IDS['model']), db.get(Video, IDS['video'])
        transcript = db.get(Transcript, IDS['transcript'])
        annotations = list(db.execute(select(ObjectModelAnnotation).where(ObjectModelAnnotation.object_id == IDS['object'])).scalars())
        segments = list(db.execute(select(Segment).where(Segment.transcript_id == IDS['transcript']).order_by(Segment.position)).scalars())
        clips = list(db.execute(select(Clip).where(Clip.video_id == IDS['video']).order_by(Clip.start_ms)).scalars())
        if (obj is None or obj.website_object_id != 'demo-cube' or user is None
                or user.email != 'demo@example.org' or model is None or model.sha256_checksum != cube_digest
                or video is None or video.stable_video_id != 'demo-colors' or video.duration_ms != 12000
                or transcript is None or len(annotations) != 3 or len(segments) != 3 or len(clips) != 3):
            raise SystemExit('Synthetic refresh requires the original seeded demo. Use ordinary replacement for personalized content.')
        if [segment.text for segment in segments] not in (legacy_text, TEXT):
            raise SystemExit('Transcript has been personalized; use ordinary replacement and review.')
        for index, (segment, clip) in enumerate(zip(segments, clips), 1):
            expected = ((index-1)*4000, index*4000)
            if (segment.id != IDS[f'segment{index}'] or clip.id != IDS[f'clip{index}']
                    or (segment.start_ms, segment.end_ms) != expected
                    or (clip.start_ms, clip.end_ms) != expected):
                raise SystemExit('Seeded clip or transcript timing changed; use ordinary replacement and review.')
        for index, title in enumerate(('Front face', 'Top edge', 'Compare two sections'), 1):
            annotation = next((a for a in annotations if a.id == IDS[f'annotation{index}']), None)
            expected_clips = [1, 3] if index == 3 else [index]
            expected_ranges = [(0, 4000), (8000, 12000)] if index == 3 else [((index-1)*4000, index*4000)]
            expected_point = [(0,0,.5),(0,.5,0),(.5,0,0)][index-1]
            if (annotation is None or annotation.title != title
                    or annotation.object_model_id != model.id or annotation.video_id != video.id
                    or (annotation.point_x,annotation.point_y,annotation.point_z) != expected_point
                    or (annotation.start_ms,annotation.end_ms) != expected_ranges[0]
                    or [(p.get('start_ms'),p.get('end_ms')) for p in annotation.playlist_json or []] != expected_ranges
                    or [p.get('clip_id') for p in annotation.playlist_json] != [str(IDS[f'clip{i}']) for i in expected_clips]):
                raise SystemExit('Seeded annotation geometry or playlist changed; use ordinary replacement and review.')
        # Generate all files before the atomic database switch. Old media remains
        # available for an operator rollback and the preservation source stays intact.
        directory = Path(settings.media_root) / settings.transcode_output_subdir
        directory.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        final = directory / f"{IDS['video']}.{digest}.{token}.mp4"
        generated = [final]
        try:
            shutil.copyfile(source, final)
            for clip in clips:
                derived = directory / f'clip-{clip.id}-{token}.mp4'
                generated.extend([derived, derived.with_suffix('.jpg')])
                generate_clip(final, derived, clip.start_ms, clip.end_ms)
                clip.output_mp4_path = str(derived)
                clip.transcript_excerpt = TEXT[clip.start_ms // 4000]
            video.playback_storage_key = final.name
            video.playback_sha256_checksum = digest
            video.playback_file_size_bytes = final.stat().st_size
            if video.title in (LEGACY_VIDEO_TITLE, CURRENT_VIDEO_TITLE):
                video.title = CURRENT_VIDEO_TITLE
            if transcript.title in (LEGACY_TRANSCRIPT_TITLE, CURRENT_TRANSCRIPT_TITLE):
                transcript.title = CURRENT_TRANSCRIPT_TITLE
            transcript.raw_text = 'WEBVTT\n\n' + '\n\n'.join(
                f'00:00:{i*4:02d}.000 --> 00:00:{(i+1)*4:02d}.000\n{text}' for i,text in enumerate(TEXT)) + '\n'
            for segment, text in zip(segments, TEXT):
                segment.text = text
            comparison = next(a for a in annotations if a.id == IDS['annotation3'])
            comparison.playlist_json = [dict(entry, label=label) for entry,label in zip(
                comparison.playlist_json, ('Front face / square', 'Compare view / circle'))]
            db.commit()
        except BaseException:
            for path in generated:
                path.unlink(missing_ok=True)
            raise
    print('Synthetic chapters refreshed. IDs, geometry, timing, review and publication states preserved.')


def replace(model: Path | None, video: Path | None) -> None:
    if settings.semantic_env.lower() != 'development':
        raise SystemExit('Replacement requires SEMANTIC_ENV=development.')
    if not model and not video:
        raise SystemExit('Supply --model and/or --video.')
    for path in (model,video):
        if path and (not path.is_file() or path.stat().st_size > settings.media_upload_max_bytes):
            raise SystemExit('Input must be a local regular file within the media upload limit.')
    with SessionLocal() as db:
        obj=db.get(Object,IDS['object'])
        user=db.get(User,IDS['user'])
        if obj is None or obj.website_object_id!='demo-cube' or user is None or user.email!='demo@example.org':
            raise SystemExit('This command requires the seeded demo database.')
        if video:
            probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(video)],capture_output=True,text=True,check=True)
            duration=int(float(json.loads(probe.stdout)['format']['duration'])*1000)
            if duration<12000:
                raise SystemExit('Demo replacement video must cover the existing 12-second timeline.')
            directory=Path(settings.media_root)/settings.transcode_output_subdir
            directory.mkdir(parents=True,exist_ok=True)
            token=uuid4().hex
            candidate=directory/f'replacement-{token}.mp4'
            try:
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(video),'-map','0:v:0','-map','0:a:0?','-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-c:a','aac','-movflags','+faststart',str(candidate)],check=True)
                with candidate.open('rb') as encoded:
                    digest=hashlib.file_digest(encoded,'sha256').hexdigest()
                final=directory/f"{IDS['video']}.{digest}.{token}.mp4"
                candidate.rename(final)
                row=db.get(Video,IDS['video'])
                row.playback_storage_key=final.name
                row.playback_sha256_checksum=digest
                row.playback_file_size_bytes=final.stat().st_size
                for clip in db.execute(select(Clip).where(Clip.video_id==row.id)).scalars():
                    derived = directory / f'clip-{clip.id}-{token}.mp4'
                    generate_clip(final, derived, clip.start_ms, clip.end_ms)
                    clip.output_mp4_path = str(derived)
                    clip.transcript_excerpt = None
                    clip.metadata_json_path = None
                    clip.citation_text = None
                row.duration_ms=duration
                row.is_published=False
                # Preservation source remains the original seed; replacement identity
                # lives in the independent playback fields used by public caching.
                for annotation in db.execute(select(ObjectModelAnnotation).where(ObjectModelAnnotation.object_id==obj.id)).scalars():
                    annotation.review_status=AnnotationReviewStatus.REVIEW_REQUIRED
                    annotation.is_published=False
                for transcript in db.execute(select(Transcript).where(Transcript.video_id==row.id)).scalars():
                    transcript.is_published=False
                db.commit()
            finally:
                candidate.unlink(missing_ok=True)
        if model:
            with model.open('rb') as stream:
                upload_object_model(obj.id,UploadFile(filename=model.name,file=stream),db,user)
    print('Replacement saved. Review annotation positions/times and update/publish the transcript through the API.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path)
    parser.add_argument('--video',type=Path)
    parser.add_argument('--synthetic-demo',type=Path,help='Refresh original seeded content from a generated demo directory, preserving review/publication states.')
    arguments=parser.parse_args()
    if arguments.synthetic_demo:
        if arguments.model or arguments.video:
            parser.error('--synthetic-demo is independent of --model and --video.')
        refresh_synthetic(arguments.synthetic_demo)
    else:
        replace(arguments.model,arguments.video)
