// Mutates only the active window's frame history; callers own pause/seek/advance.
export function guidedWindowBoundary(guided, currentMs, durationMs, decoded = false) {
  const clip = guided.clips[guided.index]
  if (!clip || clip.endMs === null) return { finish: false }
  const fullDurationEnd = Number.isFinite(durationMs) && clip.endMs >= durationMs - 1
  if ((decoded || !guided.hasDecodedFrame) && currentMs >= clip.startMs && currentMs < clip.endMs) {
    const step = currentMs - guided.lastFrameMs
    if (decoded && step > 0 && step <= 100) guided.frameStepMs = step
    guided.lastFrameMs = currentMs
    if (decoded) guided.hasDecodedFrame = true
  }
  // A decoded frame is already on screen. Stop before the following frame
  // reaches the exclusive end, retaining this final frame within the window.
  const finish = currentMs >= clip.endMs || (
    decoded && !fullDurationEnd && currentMs >= clip.startMs
    && currentMs + (guided.frameStepMs || 1000 / 30) >= clip.endMs - 0.5
  )
  const lastInside = Number.isFinite(guided.lastFrameMs)
    && guided.lastFrameMs >= clip.startMs && guided.lastFrameMs < clip.endMs
    ? guided.lastFrameMs : Math.max(clip.startMs, clip.endMs - 1000 / 30)
  return { finish, stopMs: fullDurationEnd ? durationMs : lastInside }
}
