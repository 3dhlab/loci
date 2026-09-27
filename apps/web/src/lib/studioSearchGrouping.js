/* Studio P4 — pure grouping/filtering for the persistent Studio search.
 *
 * The public segment search (`searchPublicSegments`) returns collection-wide
 * results carrying slug/stable identifiers. This module partitions those results
 * into "On this page" (the current session/recording) vs "Across the collection"
 * and applies the All / This session / This collection / Same object filters —
 * all by matching the fields the public payload already exposes, so no private
 * field is ever read. Framework-free + unit-tested; the component only renders.
 */

// Cap rendered rows per group so a large result set can never blow out layout
// or scan the whole collection client-side.
export const STUDIO_SEARCH_GROUP_CAP = 6

export const STUDIO_SEARCH_FILTERS = Object.freeze([
  { key: 'all', label: 'All' },
  { key: 'session', label: 'This session' },
  { key: 'collection', label: 'This collection' },
  { key: 'object', label: 'Same object' },
])

function str(value) {
  return typeof value === 'string' ? value : (value == null ? '' : String(value))
}

// A result belongs to the current session when its stable video id matches the
// page's (the page exposes the stable id as playback.video_id, and results carry
// stable_video_id). Falls back to nothing when either side is missing.
export function isOnPageResult(result, current = {}) {
  const resultStable = str(result?.stable_video_id)
  const pageStable = str(current?.videoStableId)
  return Boolean(resultStable) && resultStable === pageStable
}

export function isSameCollectionResult(result, current = {}) {
  const slug = str(result?.project_slug)
  return Boolean(slug) && slug === str(current?.projectSlug)
}

export function isSameObjectResult(result, current = {}) {
  const pub = str(result?.object_public_id)
  return Boolean(pub) && pub === str(current?.objectPublicId)
}

/**
 * Split results into { onPage, collection }, each bounded to `cap`. Order within
 * each group is preserved (the API already ranks). Non-array input -> empty.
 */
export function partitionStudioResults(results, current = {}, cap = STUDIO_SEARCH_GROUP_CAP) {
  const list = Array.isArray(results) ? results : []
  const onPage = []
  const collection = []
  for (const result of list) {
    if (isOnPageResult(result, current)) {
      onPage.push(result)
    } else {
      collection.push(result)
    }
  }
  return { onPage: onPage.slice(0, cap), collection: collection.slice(0, cap) }
}

/** Apply one filter key to a flat result list. Unknown keys -> unchanged. */
export function filterStudioResults(results, filterKey, current = {}) {
  const list = Array.isArray(results) ? results : []
  switch (filterKey) {
    case 'session':
      return list.filter((r) => isOnPageResult(r, current))
    case 'collection':
      return list.filter((r) => isSameCollectionResult(r, current))
    case 'object':
      return list.filter((r) => isSameObjectResult(r, current))
    case 'all':
    default:
      return list
  }
}

/**
 * Which filters are meaningful for this result set + page context. 'all' is
 * always available; a scoped filter is offered only when the page has the
 * matching identifier AND at least one result carries the field (explicit
 * degrade — an unavailable field is dropped, not silently no-op).
 */
export function availableStudioFilters(results, current = {}) {
  const list = Array.isArray(results) ? results : []
  const has = (predicate, currentKey) =>
    Boolean(str(current?.[currentKey])) && list.some(predicate)
  const enabled = new Set(['all'])
  if (has((r) => str(r?.stable_video_id), 'videoStableId')) enabled.add('session')
  if (has((r) => str(r?.project_slug), 'projectSlug')) enabled.add('collection')
  if (has((r) => str(r?.object_public_id), 'objectPublicId')) enabled.add('object')
  return STUDIO_SEARCH_FILTERS.filter((f) => enabled.has(f.key))
}

/**
 * Safe target for the "Open" verb: a same-origin relative link into the public
 * evidence surface. Rejects absolute/cross-origin URLs and any path outside
 * /evidence/objects/ so a crafted payload cannot redirect off-site.
 */
export function isSafeEvidenceUrl(url) {
  const value = str(url).trim()
  if (!value || !value.startsWith('/evidence/objects/')) {
    return false
  }
  // No protocol-relative (//host) or scheme (http:) escape.
  if (value.startsWith('//') || /^[a-z][a-z\d+.-]*:/i.test(value)) {
    return false
  }
  return true
}

/** Pick the seek target (ms) for a result — prefer the moment start. */
export function resultSeekMs(result) {
  const start = Number(result?.start_ms)
  if (Number.isFinite(start) && start >= 0) {
    return Math.floor(start)
  }
  const ctx = Number(result?.context_start_ms)
  return Number.isFinite(ctx) && ctx >= 0 ? Math.floor(ctx) : 0
}
