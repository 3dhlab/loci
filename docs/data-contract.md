# Evidence data contract

The running API's `/docs` page is the executable request-schema reference. The demo uses the same API and relational projection as the viewer.

## Geometry and annotations

Models use GLB. Annotation points and normals are in model-space coordinates; the sample cube spans −0.5 to 0.5 on each axis. Stored camera values contain position and target vectors. Replacing geometry creates a new model revision and requires placement review of existing annotations.

An annotation associates a point with a video ID and integer millisecond timing values. Optional playlist entries specify video ID, start/end milliseconds and label. Ordered entries guide playback through linked windows. Manual transcript/timeline seeking releases that guided sequence; selecting an annotation re-arms it.

## Transcript and citations

Transcript segments carry position, start/end milliseconds and text. Public transcript seeking follows the selected line's timing. Citation attribution uses public speaker/date fields when present; private observation payloads and authoring notes belong outside the public response. Synthetic examples demonstrate this boundary without real participant data.

## Media identity and delivery

Public model/video routes support complete responses and byte ranges. The demo checks content identities and exact partial bytes. Media paths are managed under the configured media root. Generated file bytes depend on FFmpeg version; content hashes derive from the actual stored bytes.

## Share state

Use the viewer's share action to copy the current annotation or video moment URL. Reloading that URL restores focus and timing through the public evidence endpoint. Embed routes use the same public object identity and configurable frame/origin policy. A shared URL exposes published content only.

See [the demo guide](../examples/README.md) for authentication, asset replacement and annotation creation. Preserve milliseconds, model-space placement and explicit publication when generating your own data.
