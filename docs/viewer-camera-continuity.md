# Camera continuity during spatial selection

Selecting a visible annotation on the model starts its evidence playback while
preserving the current rotation, zoom and OrbitControls target. The selected
annotation remains highlighted. Ambient motion stays paused while that direct
spatial selection is active, so the reader can continue examining the chosen
view during playback.

Selecting a moment through the rail requests its whole-object framing. A saved
annotation or clip link restores its spatial focus on load. Transcript and
search navigation retain their existing focus behavior. Selecting the same
annotation through the rail after a direct spatial selection also requests
framing.

The evidence host tracks a camera intent separately from the selected annotation
and increments its revision for deliberate navigation. `ModelCanvas` consumes
the direct-selection preservation intent by retaining its live camera and
pausing ambient motion. Other canvas hosts default to their existing framing.
The preservation path works with both reduced motion and ordinary motion.

## Verify against the local demo

Build and start the updated demo's web service. From `apps/web`, run:

```sh
npm test
npm run build
node tests/browser/viewer-camera-continuity.mjs
```

The browser regression uses the actual seeded demo at `http://127.0.0.1:8080`.
For each motion preference, it records camera poses, performs a real drag, and
selects the single 4–8 second pin. The check waits for playback completion and
confirms the pin, active clip, and camera pose remain in place. A manual scrub
then clears the pin and clip focus. A fresh camera drag establishes the pose
for the two-window sequence check, which confirms playback skips the gap,
retains the final pin and clip, and preserves the camera through completion.
The check waits until the Compare sequence is playing its 8–12 second window,
then switches to Top edge and confirms the new 4–8 second range continues with
its pin and clip selected. It scrubs during guided playback and confirms
playback continues past the former window boundary. Rail navigation still
requests framing for the selected annotation, and an annotation link restores
its focus.

Results and screenshots default to
`apps/web/test-results/viewer-camera-continuity`. Set `PLAYBACK_ARTIFACTS` to
choose another output directory.

For a visual check, rotate the model until the desired point is visible, release
the drag, and select its label. The video should begin its linked range while
the model retains the view you chose. A moment selected through the rail should
frame its model location.
