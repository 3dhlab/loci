# Synthetic demonstration

The demo generator constructs an eight-vertex cube from numerical coordinates,
a twelve-second silent video with three labeled cube views, a poster, three derived clip videos with posters,
and a manually authored timed transcript. All sample content is generated specifically for this demonstration. `apps/api/app/scripts/generate_demo_assets.py`
contains the complete geometry and FFmpeg recipe. `demo_storyboard.py` supplies
an original bitmap alphabet and raster geometry using Python's standard library.
The 960×540 video pairs blue, amber and purple with large chapter titles and
square, triangle and circle symbols. A moving marker traces the highlighted
cube feature, and an absolute video clock and progress bar make seeks visible.
The dark outline around highlighted features preserves their visibility across
the palette. Motion is continuous, with chapter changes every four seconds.
FFmpeg encoder versions can
change output bytes; the seed calculates identities from the actual generated files.
See the repository's sample notices for redistribution terms.

The seeded object has three annotations. “Front face” plays section 1, marked by
a square (0–4 seconds). “Top edge” plays section 2, marked by a triangle (4–8
seconds). “Compare two sections” plays the front face, followed by “Compare
view”, marked by a circle on the side face (8–12 seconds). The burned-in video
clock retains absolute source time in each derived clip. Points use model-space
coordinates and timing values use integer milliseconds. The cube spans −0.5 to
0.5 on each axis. Camera settings contain `position` and `target` vectors.

## Start and inspect

From the repository root:

```sh
python3 scripts/demo/init-env.py
docker compose build
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m app.scripts.seed_demo
docker compose up -d --wait
python3 scripts/demo/verify.py
```

Open http://localhost:8080/public. API documentation is at
http://localhost:8000/docs. The authenticated demo account is `demo@example.org`;
its generated password is `DEMO_PASSWORD` in `.env`. Repeating the seed preserves
existing content. It refuses a populated, unrelated database and shared modes.

Docker Engine with Compose v2 or Docker Desktop, Python 3 and internet access
for container images/npm/Python downloads are required. Containers run Linux;
macOS and Windows use Docker Desktop. The demo binds application ports to
127.0.0.1, stores database/media in project-scoped named volumes, Email, remote publishing, transcription and background processing have separate configuration requirements.
Use a separate Compose project (`docker compose -p another-demo ...`) and adjust
published ports before running concurrent copies.

`docker compose down` stops the demo and preserves data.
`docker compose down --volumes` deletes this project's demo database and media.
Regenerating `.env` without resetting the database changes configured credentials;
keep `.env` for the lifetime of the demo volumes.

## Authoring through the API

Authenticate with `POST /api/v1/auth/login` using JSON `email` and `password`.
Supply the returned token as `Authorization: Bearer TOKEN` on authoring calls.
The interactive `/docs` page gives exact schemas and upload forms.

- Replace geometry: `POST /api/v1/objects/{object_id}/model` with multipart `model_file`
  containing a GLB. Replacement changes the model revision and marks existing
  annotations for review; review their points before republishing.
- Add an annotation: `POST /api/v1/objects/{object_id}/model/annotations` with
  `video_id`, `title`, `point_x`, `point_y`, `point_z`, `start_ms` and `end_ms`.
  Optional `playlist` entries carry `video_id`, `clip_id`, `start_ms`, `end_ms`
  and `label`. Multi-clip evidence playback requires `clip_id` links to completed
  clips. Discover seeded clip UUIDs with authenticated
  `GET /api/v1/clips?video_id=00000000-0000-4000-8000-000000000005`.
- Publish/review an annotation with
  `PATCH /api/v1/objects/model-annotations/{annotation_id}` using
  `is_published: true` and `review_status: "ACTIVE"` after verifying its placement.
- Publish a replacement model with `PATCH /api/v1/objects/{object_id}/model`
  and `is_published: true` after reviewing associated content.

Video uploads and transcription require optional worker services and further
configuration. The supported quickstart uses the generated, normalized video.
The API verification script checks the published sample records, complete and
ranged media responses, transcript timing, and an authenticated annotation
creation/deletion round trip against the actual database.

## Substitute your own model and video

### Refresh the generated sample in an existing installation

After updating this source and rebuilding the API image, regenerate the sample
inside the running local demo and apply the explicit synthetic refresh:

```sh
docker compose build api
docker compose up -d --wait api
docker compose exec api python -m app.scripts.generate_demo_assets /tmp/loci-demo-v2
docker compose exec api python -m app.scripts.replace_demo_media --synthetic-demo /tmp/loci-demo-v2
```

This path checks the generated manifest, media checksum, codec, dimensions and
exact twelve-second duration. It also verifies the original cube checksum,
seeded IDs, annotation points and playlists, clip/segment ranges and sample
transcript. Matching original geometry can have a later reviewed model revision.
The refresh updates full and derived video files, clip posters, transcript text
and comparison labels. It also moves the original **Top edge** pin from the
top-face center `(0, 0.5, 0)` to the front top edge `(0, 0.5, 0.5)` highlighted
in the new chapter. Only that exact original pin is eligible for this correction;
customized coordinates cause the refresh to stop. The other annotation positions,
publication/review states, IDs and the 0–4/4–8/8–12 second windows stay intact. Personalized content is
directed to the ordinary replacement workflow below. New playback storage keys
and checksums invalidate cached full video. Reload the viewer after refreshing.
The original preservation source and old playback files remain available.

The refresh writes all media before switching database references in one
transaction. Take a database backup for the isolated Compose project before
running it if you need to restore the previous content.

### Supply your own content

Copy your files into the API container, then run the scoped replacement command:

```sh
docker compose cp ./my-model.glb api:/tmp/my-model.glb
docker compose cp ./my-video.mp4 api:/tmp/my-video.mp4
docker compose exec api python -m app.scripts.replace_demo_media \
  --model /tmp/my-model.glb --video /tmp/my-video.mp4
```

Either flag can be used independently. The command requires the seeded demo
object/account and development mode. Video input must cover at least twelve
seconds; FFmpeg creates a browser-compatible H.264/AAC playback file and records
its identity. The original seed source remains preserved. Video, annotation
and transcript publication are withdrawn after video substitution so their
content can be checked against the new recording. Update the transcript through
`/docs`, review annotation timing and model-space coordinates, and republish
reviewed records through their API endpoints. Model uploads use the same validated
API implementation and revision review rules as the multipart upload route.

The verification script expects the original synthetic demo content. Run it
before personalizing the installation, or in a separate disposable Compose project.

For a replacement timed transcript, use `POST /api/v1/transcripts` with JSON:

| Field | Value for the example |
| --- | --- |
| video_id | 00000000-0000-4000-8000-000000000005 |
| title | Replacement transcript |
| format | VTT |
| language | en |
| raw_text | A WEBVTT header followed by timed cues and the text for your recording. |

Enter these fields in the interactive API documentation. The first sample window runs from 00:00:00.000 to 00:00:04.000.

The returned `transcript.id` can be published with
`PATCH /api/v1/transcripts/{transcript_id}` and `{"is_published": true}` after
checking the text and timing. Manual ingestion stores usable timed segments
immediately; its queued semantic indexing job requires the optional worker.
The demo viewer works with those segments while indexing is pending.

After reviewing a substituted recording, publish its video row with
`PATCH /api/v1/videos/00000000-0000-4000-8000-000000000005` and
`{"is_published": true}`. Publish the reviewed transcript and annotations
separately. Replacing model geometry withdraws model publication; publish it
through `PATCH /api/v1/objects/{object_id}/model` only after placement review.
The public media endpoints return 404 for a withdrawn model or video.

The Compose demo sets `EMBEDDING_ENABLED=false`. Public combined-search requests
use lexical transcript matching and work directly with the stored transcript text. Optional semantic retrieval requires explicitly enabling
embeddings, selecting/configuring a provider, and running the indexing worker.
`EMBEDDING_WARMUP_ENABLED` controls startup warmup separately. The verification
script checks model replacement withdrawal, separate annotation republication,
and a real lexical search in addition to the demonstration.

Video substitution regenerates the linked clip windows from the new recording.
Their previous transcript excerpts, excerpt-file references and citation text
are cleared for review. Publishing the replacement video also releases its
regenerated clips; review those windows before publishing the video.
