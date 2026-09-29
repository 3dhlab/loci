# Generated browser fixtures

The three tracked media files in this directory are generated geometric test assets. They contain a cube, three solid-color video sections lasting twelve seconds in total, and a frame from that video. No collection objects, research recordings, or participant material are used.

Regenerate all three from [the source generator](../../../../scripts/ci/generate-browser-fixtures.py):

```sh
python3 scripts/ci/generate-browser-fixtures.py
```

Python 3 and FFmpeg with `libx264` must be on `PATH`. The script calls the same source generator used for the local demo. FFmpeg versions can encode the same generated frames into different bytes, so the browser regression checks duration, dimensions, track types, section colors, and poster pixels. It keeps an exact checksum for the generated GLB, whose bytes are stable.

The [CI boundary guard](../../../../scripts/ci/check-public-fixtures.py) checks tracked environment/runtime files, media added under this browser fixture directory, and selected synthetic citation values. It does not inspect arbitrary media content elsewhere in the repository or replace a repository-wide privacy review.
