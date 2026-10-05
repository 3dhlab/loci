"""Seed one synthetic, API-readable demo in an empty local development database."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
from uuid import UUID

from sqlalchemy import select
from app.core.config import settings
from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.entities import User, Project, Object, ObjectModel, ObjectModelAnnotation, Video, VideoStatus, Transcript, TranscriptFormat, TranscriptSource, Segment, Clip, ClipStatus
from app.scripts.generate_demo_assets import generate_assets, generate_clip
from app.scripts.demo_storyboard import CHAPTERS

IDS = {name: UUID(f'00000000-0000-4000-8000-{index:012d}') for index,name in enumerate(
    ('user','project','object','model','video','transcript','segment1','segment2','segment3','annotation1','annotation2','annotation3','clip1','clip2','clip3'),1)}
TEXT = [chapter['text'] for chapter in CHAPTERS]


def seed() -> None:
    if settings.semantic_env.lower() != 'development':
        raise SystemExit('Demo seed requires SEMANTIC_ENV=development.')
    password = os.environ.get('DEMO_PASSWORD','')
    if len(password) < 20:
        raise SystemExit('Set a generated DEMO_PASSWORD with at least 20 characters.')
    with SessionLocal() as db:
        existing = db.get(Object, IDS['object'])
        if existing:
            if existing.website_object_id != 'demo-cube':
                raise SystemExit('Demo identifier collision; refusing to modify data.')
            print('Demo already seeded. Existing content preserved.')
            return
        if db.execute(select(User.id).limit(1)).first() is not None:
            raise SystemExit('Demo seed requires an empty database. Use a separate Compose project.')
        assets=Path(settings.media_root)/'demo'
        generate_assets(assets)
        video_bytes=(assets/'colors.mp4').read_bytes()
        video_hash=hashlib.sha256(video_bytes).hexdigest()
        playback=Path(settings.media_root)/settings.transcode_output_subdir
        playback.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(assets/'colors.mp4',playback/f"{IDS['video']}.mp4")
        db.add(User(id=IDS['user'],email='demo@example.org',password_hash=get_password_hash(password),is_active=True,is_platform_admin=True))
        db.flush()
        db.add(Project(id=IDS['project'],owner_id=IDS['user'],name='Synthetic geometry',description='Generated objects and authored demonstration text.',website_slug='demo'))
        db.flush()
        db.add(Object(id=IDS['object'],project_id=IDS['project'],name='Demo cube',description='A generated cube with timed video annotations.',website_object_id='demo-cube',is_published=True,is_embed_ready=True,metadata_json={'public_standfirst':'Explore three annotations on an original geometric sample.'}))
        db.flush()
        db.add(Video(id=IDS['video'],project_id=IDS['project'],object_id=IDS['object'],stable_video_id='demo-colors',title='Three cube views',original_filename='colors.mp4',source_path=str(assets/'colors.mp4'),sha256_checksum=video_hash,playback_sha256_checksum=video_hash,playback_file_size_bytes=len(video_bytes),duration_ms=12000,status=VideoStatus.READY,is_published=True))
        db.add(ObjectModel(id=IDS['model'],object_id=IDS['object'],storage_path=str(assets/'cube.glb'),original_filename='cube.glb',mime_type='model/gltf-binary',sha256_checksum=hashlib.sha256((assets/'cube.glb').read_bytes()).hexdigest(),file_size_bytes=(assets/'cube.glb').stat().st_size,revision_number=1,default_camera_json={'position':[2,1.5,2],'target':[0,0,0]},public_poster_path=str(assets/'poster.png'),is_published=True,uploaded_by=IDS['user']))
        db.flush()
        vtt='WEBVTT\n\n'+'\n\n'.join(f'00:00:{i*4:02d}.000 --> 00:00:{(i+1)*4:02d}.000\n{text}' for i,text in enumerate(TEXT))+'\n'
        (assets/'transcript.vtt').write_text(vtt)
        db.add(Transcript(id=IDS['transcript'],video_id=IDS['video'],title='Cube view demonstration transcript',source=TranscriptSource.MANUAL,format=TranscriptFormat.VTT,language='en',raw_text=vtt,is_published=True))
        db.flush()
        for i,text in enumerate(TEXT,1):
            db.add(Segment(id=IDS[f'segment{i}'],transcript_id=IDS['transcript'],position=i-1,start_ms=(i-1)*4000,end_ms=i*4000,text=text))
        db.flush()
        for i, text in enumerate(TEXT, 1):
            clip_path = assets / f'clip-{i}.mp4'
            generate_clip(assets / 'colors.mp4', clip_path, (i-1)*4000, i*4000)
            db.add(Clip(
                id=IDS[f'clip{i}'], video_id=IDS['video'], project_id=IDS['project'],
                start_ms=(i-1)*4000, end_ms=i*4000, output_mp4_path=str(clip_path),
                website_clip_id=f'demo-clip-{i}', transcript_excerpt=text,
                citation_text=f'LOCI synthetic demonstration, section {i}.',
                status=ClipStatus.COMPLETE,
            ))
        db.flush()
        points=[(0,0,.5),(0,.5,0),(.5,0,0)]
        for i,title in enumerate(('Front face','Top edge','Compare two sections'),1):
            start,end=((i-1)*4000,i*4000)
            playlist=[{'video_id':str(IDS['video']),'clip_id':str(IDS[f'clip{i}']),'transcript_segment_id':str(IDS[f'segment{i}']),'start_ms':start,'end_ms':end,'label':title}]
            if i==3:
                start,end=0,4000
                playlist=[{'video_id':str(IDS['video']),'clip_id':str(IDS['clip1']),'transcript_segment_id':str(IDS['segment1']),'start_ms':0,'end_ms':4000,'label':'Front face / square'}, {'video_id':str(IDS['video']),'clip_id':str(IDS['clip3']),'transcript_segment_id':str(IDS['segment3']),'start_ms':8000,'end_ms':12000,'label':'Compare view / circle'}]
            x,y,z=points[i-1]
            db.add(ObjectModelAnnotation(id=IDS[f'annotation{i}'],object_model_id=IDS['model'],object_id=IDS['object'],video_id=IDS['video'],clip_id=IDS[f'clip{1 if i == 3 else i}'],transcript_segment_id=IDS[f'segment{1 if i == 3 else i}'],website_annotation_id=f'demo-annotation-{i}',title=title,description='Synthetic demonstration annotation.',point_x=x,point_y=y,point_z=z,camera_json={'position':[2,1.5,2],'target':[0,0,0]},playlist_json=playlist,start_ms=start,end_ms=end,is_published=True,model_revision_created_against=1,created_by=IDS['user']))
        db.commit()
    print('Seeded demo-cube with three annotations, including a two-clip sequence.')


if __name__=='__main__':
    seed()
