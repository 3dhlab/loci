# Generated browser fixtures

The three tracked media files in this directory are generated geometric test assets. They contain a cube, three solid-color video sections lasting twelve seconds in total, and a frame from that video. No collection objects, research recordings, or participant material are used.

Regenerate all three from [the source generator](../../../../scripts/ci/generate-browser-fixtures.py):

```sh
python3 scripts/ci/generate-browser-fixtures.py
```

Python 3 and FFmpeg with `libx264` must be on `PATH`. The script calls the same generator used by the local demo. The precise encoded video bytes can vary with FFmpeg version, while the generated content and test purpose stay the same. The [CI boundary guard](../../../../scripts/ci/check-public-fixtures.py) limits tracked browser media to these reviewed files and keeps local runtime material out of the source tree.
