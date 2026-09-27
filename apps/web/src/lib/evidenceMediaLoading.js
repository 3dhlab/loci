/**
 * Initial video loading policy for an evidence page.
 *
 * Plain object routes prioritize the interactive 3D asset and wait for a user
 * action before transferring the large session video. Explicit moment links
 * and in-app handoffs keep eager video preparation because playback context is
 * the visitor's stated intent.
 */
export function evidenceVideoPreload({ hasFocusedMoment = false, hasHandoff = false } = {}) {
  return hasFocusedMoment || hasHandoff ? 'auto' : 'none'
}

/**
 * Length the playback timeline should render.
 *
 * A deferred video reports no duration until the visitor asks for it, which would
 * leave the scrubber disabled at `--:--` for the whole life of a plain object page.
 * The published duration fills that gap. The media element wins whenever it has a
 * value, because it describes the file actually being played rather than the row
 * that described it at publication time.
 *
 * @returns {number} milliseconds, or 0 when no length is known yet.
 */
export function evidenceTimelineDurationMs({ mediaDurationMs = 0, publishedDurationMs = 0 } = {}) {
  const fromMedia = Number(mediaDurationMs)
  if (Number.isFinite(fromMedia) && fromMedia > 0) {
    return Math.floor(fromMedia)
  }
  const fromPayload = Number(publishedDurationMs)
  if (Number.isFinite(fromPayload) && fromPayload > 0) {
    return Math.floor(fromPayload)
  }
  return 0
}

/**
 * Accessible name for the session-video element.
 *
 * The published video title is the raw source filename, so it is never used: a
 * screen reader would spell out `synthetic-demo.mp4`. The object
 * title is the public, human-facing name for the same recording. The activation
 * hint is only included while the body is genuinely deferred, so the announcement
 * never tells a visitor to press play on a video that is already loading.
 */
export function evidenceVideoAccessibleName({ objectTitle = '', preload = 'none' } = {}) {
  const title = String(objectTitle || '').trim()
  const base = title ? `Session video: ${title}` : 'Session video'
  return preload === 'none' ? `${base}. Press play to load and start the recording.` : base
}

/**
 * Public display label for a session recording. Source media filenames remain
 * internal implementation details; curator-written titles continue unchanged.
 */
export function evidenceVideoDisplayTitle({ objectTitle = '', publishedTitle = '' } = {}) {
  const title = String(publishedTitle || '').trim()
  const object = String(objectTitle || '').trim()
  const looksLikeMediaFilename = /\.(?:mp4|m4v|mov|webm|mkv|avi)$/i.test(title)
  if (title && !looksLikeMediaFilename) {
    return title
  }
  return object ? `${object} session video` : 'Session video'
}
