#!/usr/bin/env python3
"""Exercise the seeded demo through the real HTTP API (standard library only)."""
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import os

base=os.environ.get('DEMO_API_URL','http://127.0.0.1:8000')
if urlsplit(base).hostname not in {'127.0.0.1','localhost'}:
    raise SystemExit('Demo verification only targets localhost.')
root=Path(__file__).resolve().parents[2]
values=dict(line.split('=',1) for line in (root/'.env').read_text().splitlines() if line and not line.startswith('#'))

def request(path, method='GET', payload=None, token=None, headers=None):
    headers=dict(headers or {})
    if token: headers['Authorization']='Bearer '+token
    if payload is not None and not isinstance(payload, bytes): headers['Content-Type']='application/json'
    req=Request(urljoin(base,path),data=payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else None,method=method,headers=headers)
    with urlopen(req,timeout=20) as response:
        raw=response.read()
        return response.status, dict(response.headers), json.loads(raw) if raw and 'application/json' in response.headers.get('Content-Type','') else raw

assert request('/health')[0]==200
page=request('/api/v1/public/objects/page')[2]
obj=next(item for item in page['items'] if item['name']=='Demo cube')
preview=request(f"/api/v1/public/objects/{obj['id']}/preview")[2]
assert len(preview['annotations'])>=3
evidence_demo=request('/api/v1/public/evidence/objects/demo-cube?annotation_id=demo-annotation-3')[2]
sequence=next(a for a in evidence_demo['annotations'] if a['id']=='demo-annotation-3')
assert sequence['related_clip_ids']==['demo-clip-1','demo-clip-3']
clip_ids={clip['id'] for clip in evidence_demo['clips']}
assert set(sequence['related_clip_ids']).issubset(clip_ids)
for clip_id in sequence['related_clip_ids']:
    assert request('/api/public/clips/'+clip_id+'/stream')[0]==200
    assert request('/api/public/clips/'+clip_id+'/poster')[0]==200
assert any(len(a['playlist'])==2 for a in preview['annotations'])
assert [(s['start_ms'],s['end_ms']) for s in preview['segments']]==[(0,4000),(4000,8000),(8000,12000)]
for key in ('model_url','video_url'):
    status,headers,body=request(preview[key]); assert status==200 and len(body)>100
    if key == 'model_url': model_bytes = body
    assert 'sha256:' in headers.get('etag',headers.get('ETag',''))
    status,headers,part=request(preview[key],headers={'Range':'bytes=0-31'})
    assert status==206 and part==body[:32]
login=request('/api/v1/auth/login','POST',{'email':'demo@example.org','password':values['DEMO_PASSWORD']})[2]
token=login['access_token']
annotation=request(f"/api/v1/objects/{obj['id']}/model/annotations",'POST',{'video_id':preview['video']['id'],'title':'API verification','point_x':0,'point_y':0,'point_z':.5,'start_ms':1000,'end_ms':2000},token)[2]
try:
    assert annotation['is_published'] is False
    public=request(f"/api/v1/public/objects/{obj['id']}/preview")[2]
    assert annotation['id'] not in [a['id'] for a in public['annotations']]
    request('/api/v1/objects/model-annotations/'+annotation['id'],'PATCH',{'is_published':True},token)
    published=request(f"/api/v1/public/objects/{obj['id']}/preview")[2]
    created=next(a for a in published['annotations'] if a['id']==annotation['id'])
    assert created['evidence_url']
    public_id=parse_qs(urlsplit(created['evidence_url']).query)['annotation'][0]
    evidence=request('/api/v1/public/evidence/objects/demo-cube?'+urlencode({'annotation_id':public_id}))[2]
    assert evidence
finally:
    assert request('/api/v1/objects/model-annotations/'+annotation['id'],'DELETE',token=token)[0]==204
# Replace the model through the multipart API, verify withdrawal, then explicitly
# review and republish the original generated geometry and annotations.
boundary='loci-demo-upload-boundary'
body=(f'--{boundary}\r\nContent-Disposition: form-data; name="model_file"; filename="cube.glb"\r\nContent-Type: model/gltf-binary\r\n\r\n'.encode()+model_bytes+f'\r\n--{boundary}--\r\n'.encode())
replacement=request(f"/api/v1/objects/{obj['id']}/model",'POST',body,token,{'Content-Type':f'multipart/form-data; boundary={boundary}'})[2]
assert replacement['is_published'] is False
try:
    request(f"/api/v1/public/objects/{obj['id']}/model/file")
    raise AssertionError('Replacement model remained public before review')
except HTTPError as error:
    assert error.code==404
withdrawn=request(f"/api/v1/public/objects/{obj['id']}/preview")[2]
assert withdrawn['model'] is None and not withdrawn['annotations']
request(f"/api/v1/objects/{obj['id']}/model",'PATCH',{'is_published':True},token)
assert not request(f"/api/v1/public/objects/{obj['id']}/preview")[2]['annotations']
for original in preview['annotations']:
    request('/api/v1/objects/model-annotations/'+original['id'],'PATCH',{'review_status':'ACTIVE','is_published':True},token)
assert len(request(f"/api/v1/public/objects/{obj['id']}/preview")[2]['annotations'])==len(preview['annotations'])
search=request('/api/v1/public/search/segments','POST',{'query':'cube','retrieval_mode':'combined'})[2]
assert search['total_results']>0
print('PASS: real API demo, three annotations, two-clip sequence, transcript, media/ranges, authenticated creation, public privacy, published evidence URL, model replacement review and lexical search.')
