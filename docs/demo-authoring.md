# Visual authoring in the local demo

[Watch the 3D Humanities Lab walkthrough](media/demo-authoring/loci-demo-authoring.mp4)
or read its [timed text description](media/demo-authoring/transcript.md).
Use this source checkout for the workflow below; the packaged v0.1.1 release
predates the optional console override and new chapter media.

This source includes a signed-in visual console for selecting a video range,
placing an annotation on a model, reviewing it, and making it visible to local
readers. The default quickstart serves the public reader. Enable the console
with the optional Compose override after completing the quickstart:

```sh
docker compose -f docker-compose.yml -f docker-compose.authoring.yml build web
docker compose -f docker-compose.yml -f docker-compose.authoring.yml up -d --no-deps --wait web
```

Use `docker-compose` in place of `docker compose` if your installation supplies
the standalone command. The override changes the web build's application surface
to `console`. The same Nginx configuration proxies `/api` to the existing API;
the database, media and other services retain their existing setup.

Open [the console](http://localhost:8080/console). Sign in as `demo@example.org`
with the generated `DEMO_PASSWORD` from your local `.env`. Keep that file private.
[Public browse](http://localhost:8080/public) and the public evidence routes
continue to work in this build. `/preview` opens the authenticated published
preview. The console entry route is `/console`.

## Create a single-range annotation on the sample cube

1. In **Analysis**, open the sample video **Three cube views**. Select its video
   in the player, or search for `front face` with **Transcript only** and choose
   **Open in player** on the result.
2. In **Clip Export**, set **Start ms** to `0` and **End ms** to `4000`. You can
   also seek the player and use **Set start from current time** and **Set end
   from current time** to capture boundaries.
3. Choose **Map to 3D**. The console opens the **3D Model** workspace with the
   cube and the selected range in the **Annotation Editor**.
4. Choose **Place new pin for this range**. Rotate the cube to expose the desired
   face, then click its surface. Confirm **Placement point: Captured** and
   the notice that the point was captured.
5. Enter a title such as `My front-face observation`, confirm the linked video
   and `0`–`4000` range, and choose **Create annotation**. The new annotation
   is private. The existing sample annotations remain available.
6. In the **Annotations** list, choose **Preview** beside your new annotation.
   Review the point and playback range. The private preview plays the annotation
   before reader publication.
7. Choose **Publish** beside that annotation. This makes the reviewed annotation
   visible through the existing published sample cube. **Publish package** also
   publishes the object, model, selected video/transcript and all its annotations;
   use the individual annotation control for this exercise.
8. Open [the cube's public evidence page](http://localhost:8080/evidence/objects/demo-cube)
   in a separate tab and reload it. Select your new annotation. Confirm that its
   point matches your placement and video playback starts at the front-face
   chapter and stops at four seconds. The single-range reader path uses the
   published full video and the annotation's start/end window.

Reader publication here changes visibility in the local installation. DOI
registration, archival deposit and remote synchronization are separate workflows.
The sample already has the public object identity, published model/video and
transcript required by this exercise.

To clean up the exercise, use **Unpublish** or **Delete** beside your new
annotation in the console. To restore the public-only build:

```sh
docker compose build web
docker compose up -d --no-deps --wait web
```

## Scope and remaining work

Single-range mapping and placement exist in the current console. This guide
exposes them through an opt-in local build. The real-API browser check
`npm run test:demo:authoring` covers range selection, captured placement,
private creation and preview, individual publication, reader playback and cleanup.
Run it only against a disposable seeded demo with the console build enabled.
It reads the generated demo password from the local `.env`; it never writes that
password or the login token to its diagnostics. The required clean-demo CI job
runs this check after its public-reader checks.

The following areas need additional development for a broader authoring product:

- **Multi-range publication:** mapping currently creates ranges with empty
  `clip_id` links. The public viewer's multi-clip sequences use completed,
  publicly identified clip records. The default demo runs four services and
  omits the optional export worker. A complete workflow needs clip export,
  stable public clip IDs and an editor control that attaches completed clips.
- **New collection objects:** the current object creation/update API exposes
  metadata and publication state. Canonical evidence pages additionally need
  a stable `website_object_id` and `is_embed_ready` state. The seeded cube
  supplies those values. A general object-to-reader workflow needs supported
  identity and readiness provisioning.

The API forms at [localhost:8000/docs](http://localhost:8000/docs) remain available
for explicit record authoring and review.
