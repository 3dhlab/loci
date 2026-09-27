/* Evidence direct-load scroll policy — pure, framework-free.
 *
 * P2 (mobile/tablet QA 2026-07-09): a PLAIN object URL (e.g.
 * `/evidence/objects/synthetic-vessel`) must land at the TOP of the page.
 * Auto-scroll to a moment (transcript row / rail item) on initial load should
 * happen ONLY for an explicit deep link that names a moment — an annotation,
 * clip, video, a `?t=` timestamp, or a `?t_scroll=` transcript-scroll restore.
 *
 * `t` is retained as a validated digit string by the route parser; `tScroll` is
 * parsed to a number. Zero remains a valid explicit deep link ("jump to start").
 * Ongoing follow-along during playback is outside this helper; it only decides
 * whether the first on-load auto-scroll is allowed to run.
 */

function isFiniteTimestampValue(value) {
  if (typeof value === 'number') {
    return Number.isFinite(value)
  }
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    return Number.isFinite(Number(value))
  }
  return false
}

// Does the route explicitly name a moment (annotation / clip / video / t)?
export function hasExplicitMomentDeepLink({ annotation, clip, video, t } = {}) {
  return Boolean(annotation) || Boolean(clip) || Boolean(video) || isFiniteTimestampValue(t)
}

// Should the evidence page auto-scroll to the active moment on initial load?
// True only for an explicit moment deep link or an explicit transcript-scroll
// restore; false for a plain object URL (which must land at the top).
export function shouldAutoScrollOnLoad({ annotation, clip, video, t, tScroll } = {}) {
  return hasExplicitMomentDeepLink({ annotation, clip, video, t }) || Number.isFinite(tScroll)
}

// Center a rail item inside its own horizontal viewport. The returned value is
// clamped to the element's scroll range so callers can pass it directly to
// `scrollTo({ left })` without invoking page-level scroll behavior.
export function centeredRailScrollLeft({
  itemOffsetLeft,
  itemOffsetWidth,
  trackClientWidth,
  trackScrollWidth,
} = {}) {
  const itemLeft = Number(itemOffsetLeft)
  const itemWidth = Number(itemOffsetWidth)
  const viewportWidth = Number(trackClientWidth)
  const contentWidth = Number(trackScrollWidth)

  if (
    !Number.isFinite(itemLeft)
    || !Number.isFinite(itemWidth)
    || !Number.isFinite(viewportWidth)
    || !Number.isFinite(contentWidth)
  ) {
    return 0
  }

  const maxScrollLeft = Math.max(0, contentWidth - viewportWidth)
  const centered = itemLeft + (itemWidth / 2) - (viewportWidth / 2)
  return Math.min(maxScrollLeft, Math.max(0, centered))
}
