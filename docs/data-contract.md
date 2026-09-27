# Evidence data contract

The API documentation at `/docs` describes accepted fields and provides forms for trying requests. The demonstration and viewer use the same database records and API.

## Geometry and annotations

Models use GLB. Annotation points and normals are in model-space coordinates; the sample cube spans −0.5 to 0.5 on each axis. Stored camera values contain position and target vectors. Replacing geometry creates a new model revision and requires placement review of existing annotations.

An annotation associates a point with a video ID and integer millisecond timing values. Optional playlist entries specify the video and clip IDs, start and end times in milliseconds, and a label. Ordered entries guide playback through linked windows. Selecting a transcript line or moving along the timeline switches to manual playback. Selecting an annotation starts its guided sequence again.

## Transcript and citations

Transcript segments carry position, start/end milliseconds and text. Public transcript seeking follows the selected line's timing. Citation attribution uses public speaker/date fields when present; private observation payloads and authoring notes belong outside the public response. Generated examples demonstrate the separation between authoring information and published content.

## Media identity and delivery

Public model/video routes support complete responses and byte ranges. The demo checks content identities and exact partial bytes. Media paths are managed under the configured media root. Generated file bytes depend on FFmpeg version; content hashes derive from the actual stored bytes.

## Share state

Use the viewer's share action to copy a link. An annotation link opens the annotation at its start. A selected clip link restores that clip's start and its annotation context. A video-moment link includes the playback timestamp. Opening the link restores the corresponding selection and timing. Embed routes use the same public object identity and configurable frame/origin policy. A shared URL exposes published content only.

See [the demo guide](../examples/README.md) for authentication, asset replacement and annotation creation. Preserve milliseconds, model-space placement and explicit publication when generating your own data.
