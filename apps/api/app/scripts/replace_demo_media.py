"""Replace media in the seeded local demo and require review of linked content."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import select
from app.api.v1.endpoints.object_models import upload_object_model
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import Object, ObjectModelAnnotation, AnnotationReviewStatus, Transcript, User, Video, Clip
from app.scripts.seed_demo import IDS
from app.scripts.generate_demo_assets import generate_clip


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
    arguments=parser.parse_args()
    replace(arguments.model,arguments.video)
