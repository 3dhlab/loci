"""Generate original geometric sample media using the standard library and FFmpeg."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from app.scripts.demo_storyboard import CHAPTERS, FPS, HEIGHT, VERSION, WIDTH, chapter_base, render_frame


def generate_assets(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    vertices = [(-.5,-.5,-.5),(.5,-.5,-.5),(.5,.5,-.5),(-.5,.5,-.5),
                (-.5,-.5,.5),(.5,-.5,.5),(.5,.5,.5),(-.5,.5,.5)]
    indices = [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,3,7,6,3,6,2,0,4,7,0,7,3,1,2,6,1,6,5]
    binary = b''.join(struct.pack('<3f', *v) for v in vertices) + struct.pack('<36H', *indices)
    document = {'asset': {'version':'2.0','generator':'LOCI geometric demo generator'},
        'scene':0,'scenes':[{'nodes':[0]}],'nodes':[{'mesh':0}],
        'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1,'material':0}]}],
        'materials':[{'pbrMetallicRoughness':{'baseColorFactor':[.12,.55,.72,1],'metallicFactor':0,'roughnessFactor':.8},'doubleSided':True}],
        'buffers':[{'byteLength':len(binary)}],
        'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':96,'target':34962},{'buffer':0,'byteOffset':96,'byteLength':72,'target':34963}],
        'accessors':[{'bufferView':0,'componentType':5126,'count':8,'type':'VEC3','min':[-.5]*3,'max':[.5]*3},{'bufferView':1,'componentType':5123,'count':36,'type':'SCALAR'}]}
    encoded = json.dumps(document,separators=(',',':')).encode()
    encoded += b' ' * (-len(encoded)%4)
    glb = struct.pack('<4sII',b'glTF',2,28+len(encoded)+len(binary))
    glb += struct.pack('<I4s',len(encoded),b'JSON')+encoded
    glb += struct.pack('<I4s',len(binary),b'BIN\0')+binary
    (destination/'cube.glb').write_bytes(glb)
    # Semantic labels, shape badges and a moving marker make every seek visible.
    # Keep the established filename and 0/4/8/12 second boundaries stable.
    bases = tuple(chapter_base(i) for i in range(len(CHAPTERS)))
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','rawvideo','-pixel_format','rgb24','-video_size',f'{WIDTH}x{HEIGHT}',
        '-framerate',str(FPS),'-i','pipe:0','-an','-c:v','libx264',
        '-pix_fmt','yuv420p','-threads','1','-movflags','+faststart',str(destination/'colors.mp4')]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
        assert encoder.stdin is not None
        try:
            for frame in range(FPS * 12):
                encoder.stdin.write(render_frame(frame, bases))
        finally:
            encoder.stdin.close()
        if encoder.wait():
            raise subprocess.CalledProcessError(encoder.returncode, command)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(destination/'colors.mp4'),
        '-frames:v','1','-threads','1',str(destination/'poster.png')],check=True)
    manifest = {'version': VERSION, 'width': WIDTH, 'height': HEIGHT, 'fps': FPS,
                'duration_ms': 12000, 'chapters': CHAPTERS,
                'cube_sha256': hashlib.sha256(glb).hexdigest(),
                'video_sha256': hashlib.sha256((destination/'colors.mp4').read_bytes()).hexdigest()}
    (destination/'demo-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def generate_clip(source: Path, destination: Path, start_ms: int, end_ms: int) -> None:
    """Encode a bounded clip and its poster from a local normalized video."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-ss', str(start_ms / 1000), '-i', str(source),
        '-t', str((end_ms - start_ms) / 1000), '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-threads', '1',
        '-c:a', 'aac', '-movflags', '+faststart', str(destination),
    ], check=True)
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-i', str(destination), '-frames:v', '1', '-threads', '1',
        str(destination.with_suffix('.jpg')),
    ], check=True)


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    generate_assets(parser.parse_args().destination)
