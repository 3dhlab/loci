import { guidedWindowBoundary } from '../lib/guidedWindowBoundary'
import { semanticFitTier } from '../lib/searchPresentation'
import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import BrandLockup from '../components/BrandLockup'
import BrandMotion from '../components/BrandMotion'
import ErrorState, { buildErrorState } from '../components/ErrorState'
import SearchAutosuggest from '../components/SearchAutosuggest'
import EvidenceLoadingState, { EVIDENCE_MAIN_CONTENT_ID } from './EvidenceLoadingState'
import StudioShell from './studio/StudioShell'
import StudioSearch from './studio/StudioSearch'
import { isStudioSurfaceEnabled, resolveStudioPaletteSync } from '../public/studioSurface'
import { isSafeEvidenceUrl } from '../lib/studioSearchGrouping'
import { getPublicEvidencePage, resolveApiUrl, resolvePublicObjectPosterUrl, searchPublicSegments } from '../lib/api'
import { useModelLoadPolicy } from '../lib/modelLoadPolicy'
import { buildModelRequestUrl } from '../lib/modelVariantUrl'
import {
  evidenceTimelineDurationMs,
  evidenceVideoAccessibleName,
  evidenceVideoDisplayTitle,
  evidenceVideoPreload,
} from '../lib/evidenceMediaLoading'
import { shouldAutoScrollOnLoad } from '../lib/evidenceScroll'
import { resolveHydratedAnnotationId } from '../lib/evidenceSelection'
import { PUBLIC_BROWSE_PATH, navigateToPublicBrowse, readWindowLocation } from '../lib/navigation'
import {
  normalizeQueryValue,
  normalizeFiniteNumber,
  collapseWhitespace,
  normalizeCitationAttribution,
  selectCitationAttributionForFocus,
  buildPlainTextEvidenceCitation,
  buildFormattedEvidenceCitation,
} from '../lib/citationAttribution'

const EVIDENCE_TABLET_MEDIA_QUERY = '(max-width: 1024px)'
const EVIDENCE_MOBILE_MEDIA_QUERY = '(max-width: 760px)'
const EVIDENCE_MODEL_CANVAS_DPR = [1, 1.5]
const EVIDENCE_PLAYBACK_RENDER_INTERVAL_MS = 250
const EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM = 't_scroll'
const EVIDENCE_TRANSCRIPT_SCROLL_SYNC_DEBOUNCE_MS = 200
const EVIDENCE_TRANSCRIPT_SCROLL_RESTORE_TOLERANCE_PX = 8
const CITATION_FORMAT_OPTIONS = [
  { id: 'apa', label: 'APA 7' },
  { id: 'chicago', label: 'Chicago' },
  { id: 'harvard', label: 'Harvard' },
  { id: 'turabian', label: 'Turabian' }
]

const LazyModelCanvas = lazy(() => import('../components/ModelCanvas'))

// P3 modal isolation: inert + aria-hide every element that is NOT on the
// dialog's ancestor chain, so background controls (including the page-level
// "Copy citation" trigger) leave the accessibility/focus tree while the dialog
// is open — eliminating the duplicate accessible button. Works for both the
// standard (dialog is a sibling of <main>) and Studio (dialog nested inside
// .studio-shell) DOM shapes because it disables each ancestor level's siblings.
// Live regions (aria-live / status / alert) are skipped so copy announcements
// still reach screen readers. Returns a restore function; a no-op without a
// dialog element or `inert` support degrades gracefully to aria-hidden only.
function applyModalBackgroundIsolation(dialogEl) {
  if (!dialogEl || typeof document === 'undefined') {
    return () => {}
  }
  const changed = []
  let node = dialogEl
  while (node && node !== document.body && node.parentElement) {
    const parent = node.parentElement
    for (const sibling of Array.from(parent.children)) {
      if (sibling === node || !(sibling instanceof HTMLElement)) {
        continue
      }
      if (sibling.matches('[aria-live], [role="status"], [role="alert"]')) {
        continue
      }
      changed.push({
        el: sibling,
        prevInert: sibling.inert,
        hadAriaHidden: sibling.hasAttribute('aria-hidden'),
        prevAriaHidden: sibling.getAttribute('aria-hidden'),
      })
      sibling.inert = true
      sibling.setAttribute('aria-hidden', 'true')
    }
    node = parent
  }
  return () => {
    for (const entry of changed) {
      entry.el.inert = entry.prevInert
      if (entry.hadAriaHidden) {
        entry.el.setAttribute('aria-hidden', entry.prevAriaHidden)
      } else {
        entry.el.removeAttribute('aria-hidden')
      }
    }
  }
}

function focusSkipTarget(targetId) {
  if (typeof document === 'undefined') {
    return
  }

  const target = document.getElementById(targetId)
  if (!(target instanceof HTMLElement)) {
    return
  }

  window.requestAnimationFrame(() => {
    target.focus()
  })
}

function buildMotionDelayStyle(delayMs = 0) {
  return {
    '--motion-delay': `${Math.max(0, delayMs)}ms`
  }
}

function extractWebsiteObjectId(pathname) {
  const segments = pathname.split('/').filter(Boolean)
  if (segments[0] !== 'evidence' || segments[1] !== 'objects') {
    return ''
  }
  return decodeURIComponent(segments[2] || '')
}

function parseTimestampValue(value) {
  const trimmed = normalizeQueryValue(value)
  if (!trimmed) {
    return ''
  }
  if (!/^\d+$/.test(trimmed)) {
    return null
  }
  return trimmed
}

function parseTranscriptScrollValue(value) {
  const trimmed = normalizeQueryValue(value)
  if (!trimmed || !/^\d+$/.test(trimmed)) {
    return null
  }

  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : null
}

function buildEvidenceRequestKey(websiteObjectId, params) {
  return [
    websiteObjectId,
    normalizeQueryValue(params?.clip),
    normalizeQueryValue(params?.annotation),
    normalizeQueryValue(params?.video),
    normalizeQueryValue(params?.t),
  ].join('::')
}

function parseEvidenceRoute(pathname, search) {
  const websiteObjectId = extractWebsiteObjectId(pathname)
  const searchParams = new URLSearchParams(search)
  const viewState = {
    tScroll: parseTranscriptScrollValue(searchParams.get(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM))
  }

  if (!websiteObjectId) {
    return {
      websiteObjectId: '',
      params: { clip: '', annotation: '', video: '', t: '' },
      viewState,
      requestKey: '',
      errorState: buildErrorState(null, {
        kind: 'parse',
        parseDetail: 'This evidence link does not point to a published object.'
      })
    }
  }

  const params = {
    clip: normalizeQueryValue(searchParams.get('clip')),
    annotation: normalizeQueryValue(searchParams.get('annotation')),
    annotationContext: normalizeQueryValue(searchParams.get('annotation_context')),
    video: normalizeQueryValue(searchParams.get('video')),
    t: parseTimestampValue(searchParams.get('t'))
  }

  if (params.t === null) {
    return {
      websiteObjectId,
      params: { ...params, t: '' },
      viewState,
      requestKey: buildEvidenceRequestKey(websiteObjectId, { ...params, t: '' }),
      errorState: buildErrorState(null, {
        kind: 'parse',
        parseDetail: 'This evidence link includes an invalid time code.'
      })
    }
  }

  const hasClip = Boolean(params.clip)
  const hasAnnotation = Boolean(params.annotation)
  const hasVideo = Boolean(params.video)
  const hasTimestamp = Boolean(params.t)

  if (hasClip && (hasAnnotation || hasVideo || hasTimestamp)) {
    return {
      websiteObjectId,
      params,
      viewState,
      requestKey: buildEvidenceRequestKey(websiteObjectId, params),
      errorState: buildErrorState(null, {
        kind: 'parse',
        parseDetail: 'This evidence link includes conflicting moment settings.'
      })
    }
  }

  if (hasAnnotation && (hasVideo || hasTimestamp)) {
    return {
      websiteObjectId,
      params,
      viewState,
      requestKey: buildEvidenceRequestKey(websiteObjectId, params),
      errorState: buildErrorState(null, {
        kind: 'parse',
        parseDetail: 'This evidence link includes conflicting moment settings.'
      })
    }
  }

  return {
    websiteObjectId,
    params,
    viewState,
    requestKey: buildEvidenceRequestKey(websiteObjectId, params),
    errorState: null
  }
}

function normalizeTranscriptScrollTop(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0
}

function replaceEvidenceViewUrl({ transcriptScrollTop = null } = {}) {
  if (typeof window === 'undefined') {
    return
  }

  const url = new URL(window.location.href)
  const normalizedScrollTop = Number.isFinite(Number(transcriptScrollTop))
    ? normalizeTranscriptScrollTop(transcriptScrollTop)
    : null

  if (normalizedScrollTop && normalizedScrollTop > 0) {
    url.searchParams.set(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM, String(normalizedScrollTop))
  } else {
    url.searchParams.delete(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM)
  }

  const nextLocation = `${url.pathname}${url.search}${url.hash}`
  const currentLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`
  if (nextLocation === currentLocation) {
    return
  }

  const historyState = window.history.state && typeof window.history.state === 'object'
    ? window.history.state
    : null
  window.history.replaceState(historyState, '', nextLocation)
}

function replaceEvidenceCanonicalUrl(canonicalUrl) {
  if (typeof window === 'undefined') {
    return
  }

  const normalizedCanonicalUrl = normalizeQueryValue(canonicalUrl)
  if (!normalizedCanonicalUrl) {
    return
  }

  const currentUrl = new URL(window.location.href)
  const nextUrl = new URL(normalizedCanonicalUrl, currentUrl.origin)
  const transcriptScrollTop = currentUrl.searchParams.get(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM)
  const annotationContext = currentUrl.searchParams.get('annotation_context')

  if (transcriptScrollTop && !nextUrl.searchParams.has(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM)) {
    nextUrl.searchParams.set(EVIDENCE_TRANSCRIPT_SCROLL_QUERY_PARAM, transcriptScrollTop)
  }
  // Preserve the Studio flag through canonical-URL normalization so a shared
  // `?studio=1` link stays in Studio mode after the evidence page loads.
  const studioFlag = currentUrl.searchParams.get('studio')
  if (studioFlag && !nextUrl.searchParams.has('studio')) {
    nextUrl.searchParams.set('studio', studioFlag)
  }
  if (annotationContext && !nextUrl.searchParams.has('annotation_context')) {
    nextUrl.searchParams.set('annotation_context', annotationContext)
  }
  if (currentUrl.hash && !nextUrl.hash) {
    nextUrl.hash = currentUrl.hash
  }

  const nextLocation = `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`
  const currentLocation = `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`
  if (nextLocation === currentLocation) {
    return
  }

  const historyState = window.history.state && typeof window.history.state === 'object'
    ? window.history.state
    : null
  window.history.replaceState(historyState, '', nextLocation)
}

function stripEvidenceFocusParams(canonicalUrl) {
  const normalizedCanonicalUrl = normalizeQueryValue(canonicalUrl)
  if (!normalizedCanonicalUrl) {
    return ''
  }

  try {
    const baseOrigin = typeof window !== 'undefined' ? window.location.origin : 'https://example.invalid'
    const nextUrl = new URL(normalizedCanonicalUrl, baseOrigin)
    nextUrl.searchParams.delete('annotation')
    nextUrl.searchParams.delete('clip')
    nextUrl.searchParams.delete('video')
    nextUrl.searchParams.delete('t')
    return `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`
  } catch {
    return normalizedCanonicalUrl
  }
}

function buildEvidencePageErrorState(error) {
  return buildErrorState(error, {
    fallbackKind: 'network',
    networkDetail: 'The published evidence page is temporarily unavailable.',
    notFoundDetail: 'This published evidence page could not be found.',
    unauthorizedDetail: 'This evidence page requires authorization.'
  })
}

function describeEvidenceSearchError(error) {
  const errorState = buildErrorState(error, {
    fallbackKind: 'network',
    networkDetail: 'Search is temporarily unavailable for this recording.',
    notFoundDetail: 'This recording search is no longer available.',
    unauthorizedDetail: 'This recording search requires authorization.'
  })

  return errorState.detail
}

function reloadProtectedEvidencePage() {
  window.location.assign(window.location.href)
}

function readEvidenceHandoffState(websiteObjectId) {
  if (typeof window === 'undefined') {
    return null
  }

  const handoff = window.history.state?.semanticEvidenceHandoff
  if (!handoff || typeof handoff !== 'object') {
    return null
  }

  const handoffObjectId = normalizeQueryValue(handoff.websiteObjectId)
  if (!handoffObjectId || (websiteObjectId && handoffObjectId !== websiteObjectId)) {
    return null
  }

  const normalizedSeekMs = Number.isFinite(Number(handoff.seekMs))
    ? Math.max(0, Math.floor(Number(handoff.seekMs)))
    : null
  const handoffMoment = handoff.moment && typeof handoff.moment === 'object' ? handoff.moment : null

  return {
    websiteObjectId: handoffObjectId,
    annotationId: normalizeQueryValue(handoff.annotationId),
    videoId: normalizeQueryValue(handoff.videoId),
    seekMs: normalizedSeekMs,
    autoplay: Boolean(handoff.autoplay),
    source: normalizeQueryValue(handoff.source),
    moment: handoffMoment ? {
      query: normalizeQueryValue(handoffMoment.query),
      retrievalMode: normalizeQueryValue(handoffMoment.retrievalMode) || 'transcript_only',
      text: normalizeQueryValue(handoffMoment.text),
      sceneDescription: normalizeQueryValue(handoffMoment.sceneDescription),
      thumbnailUrl: normalizeQueryValue(handoffMoment.thumbnailUrl),
      videoTitle: normalizeQueryValue(handoffMoment.videoTitle),
      momentStartMs: normalizeFiniteNumber(handoffMoment.momentStartMs),
      momentEndMs: normalizeFiniteNumber(handoffMoment.momentEndMs),
      contextStartMs: normalizeFiniteNumber(handoffMoment.contextStartMs),
      contextEndMs: normalizeFiniteNumber(handoffMoment.contextEndMs),
    } : null
  }
}

function formatClock(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }

  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function formatFocusWindow(focus) {
  if (!focus) {
    return ''
  }

  if (Number.isFinite(focus.end_ms) && focus.end_ms > focus.start_ms) {
    return `${formatClock(focus.start_ms)} - ${formatClock(focus.end_ms)}`
  }

  return formatClock(focus.seek_ms)
}

function truncateText(value, maxLength = 220) {
  const normalized = collapseWhitespace(value)
  if (!normalized) {
    return ''
  }

  if (normalized.length <= maxLength) {
    return normalized
  }

  return `${normalized.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`
}

function formatCitationAccessDate(value) {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }

  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  }).format(date)
}

function resolveAbsolutePublicUrl(url) {
  const normalized = normalizeQueryValue(url)
  if (!normalized || typeof window === 'undefined') {
    return normalized
  }

  try {
    return new URL(normalized, window.location.origin).toString()
  } catch {
    return normalized
  }
}

function buildCanonicalEvidenceMomentUrl({
  canonicalUrl,
  websiteObjectId,
  source,
  annotationId,
  clipId,
  videoId,
  seekMs
}) {
  const normalizedCanonicalUrl = normalizeQueryValue(canonicalUrl)
  const normalizedObjectId = normalizeQueryValue(websiteObjectId)
  const basePath = stripEvidenceFocusParams(normalizedCanonicalUrl)
    || (normalizedCanonicalUrl ? normalizedCanonicalUrl : normalizedObjectId ? `/evidence/objects/${encodeURIComponent(normalizedObjectId)}` : '')

  if (!basePath || typeof window === 'undefined') {
    return basePath
  }

  try {
    const nextUrl = new URL(basePath, window.location.origin)
    nextUrl.search = ''
    nextUrl.hash = ''
    const studioFlag = new URLSearchParams(window.location.search).get('studio')
    if (studioFlag) {
      nextUrl.searchParams.set('studio', studioFlag)
    }

    const resolvedSource = normalizeQueryValue(source)
    const resolvedAnnotationId = normalizeQueryValue(annotationId)
    const resolvedClipId = normalizeQueryValue(clipId)
    const resolvedVideoId = normalizeQueryValue(videoId)
    const resolvedSeekMs = Number.isFinite(Number(seekMs))
      ? Math.max(0, Math.floor(Number(seekMs)))
      : 0

    if (resolvedSource === 'clip' && resolvedClipId) {
      nextUrl.searchParams.set('clip', resolvedClipId)
      if (resolvedAnnotationId) {
        nextUrl.searchParams.set('annotation_context', resolvedAnnotationId)
      }
    } else if ((resolvedSource === 'annotation' || resolvedSource === 'default') && resolvedAnnotationId) {
      nextUrl.searchParams.set('annotation', resolvedAnnotationId)
    } else if (resolvedVideoId) {
      nextUrl.searchParams.set('video', resolvedVideoId)
      if (resolvedSeekMs > 0) {
        nextUrl.searchParams.set('t', String(resolvedSeekMs))
      }
    }

    return nextUrl.toString()
  } catch {
    return resolveAbsolutePublicUrl(basePath)
  }
}

async function copyTextToClipboard(text) {
  const normalized = normalizeQueryValue(text)
  if (!normalized) {
    return false
  }

  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(normalized)
      return true
    } catch {
      // Fall through to the textarea fallback when clipboard permissions fail.
    }
  }

  if (typeof document === 'undefined' || !document.body) {
    return false
  }

  const target = document.createElement('textarea')
  target.value = normalized
  target.setAttribute('readonly', '')
  target.style.position = 'fixed'
  target.style.top = '0'
  target.style.left = '0'
  target.style.width = '1px'
  target.style.height = '1px'
  target.style.opacity = '0'
  document.body.appendChild(target)
  target.focus()
  target.select()
  target.setSelectionRange(0, target.value.length)

  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }

  document.body.removeChild(target)
  return copied
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function resultHasSceneSupport(result) {
  if (!result || typeof result !== 'object') {
    return false
  }

  return result.visual_score != null
    || Boolean(normalizeQueryValue(result.scene_description))
    || Boolean(normalizeQueryValue(result.thumbnail_url))
}

function publicVideoIdFromStreamUrl(streamUrl) {
  const normalized = normalizeQueryValue(streamUrl)
  if (!normalized) {
    return ''
  }

  const match = normalized.match(/\/videos\/([^/]+)\/stream/i)
  return match?.[1] || ''
}

function focusLabel(source) {
  switch (source) {
    case 'clip':
      return 'Published clip'
    case 'annotation':
      return 'Published moment'
    case 'video':
      return 'Video context'
    case 't':
      return 'Timestamp focus'
    default:
      return 'Object default'
  }
}

function surroundingContextRange(result) {
  const momentStart = Math.max(0, normalizeFiniteNumber(result?.start_ms) ?? normalizeFiniteNumber(result?.momentStartMs) ?? 0)
  const momentEnd = Math.max(momentStart, normalizeFiniteNumber(result?.end_ms) ?? normalizeFiniteNumber(result?.momentEndMs) ?? momentStart)
  const contextStart = Math.max(0, normalizeFiniteNumber(result?.context_start_ms) ?? normalizeFiniteNumber(result?.contextStartMs) ?? momentStart)
  const contextEnd = Math.max(contextStart, normalizeFiniteNumber(result?.context_end_ms) ?? normalizeFiniteNumber(result?.contextEndMs) ?? momentEnd)

  return {
    momentStart,
    momentEnd,
    contextStart,
    contextEnd,
  }
}

function searchSceneSupportText(result) {
  const sceneDescription = normalizeQueryValue(result?.scene_description)
  if (sceneDescription) {
    return sceneDescription
  }

  if (!resultHasSceneSupport(result)) {
    return ''
  }

  return 'Visual support is available for this transcript moment.'
}

function searchMatchBasisLine(result) {
  return resultHasSceneSupport(result)
    ? 'Matched in the transcript with scene support.'
    : 'Matched in the transcript.'
}

function searchResultContextDetails(result) {
  return [
    { label: 'Object', value: normalizeQueryValue(result?.object_name) },
    { label: 'Collection', value: normalizeQueryValue(result?.project_name) },
  ].filter((detail) => detail.value)
}

function annotationsToCanvas(annotations) {
  if (!Array.isArray(annotations)) {
    return []
  }

  return annotations.map((annotation) => ({
    id: annotation.id,
    title: annotation.title,
    description: annotation.description || null,
    point_x: annotation.point_x,
    point_y: annotation.point_y,
    point_z: annotation.point_z,
    normal_x: annotation.normal_x ?? null,
    normal_y: annotation.normal_y ?? null,
    normal_z: annotation.normal_z ?? null,
    camera_json: annotation.camera || null,
    related_clip_ids: Array.isArray(annotation.related_clip_ids) ? annotation.related_clip_ids : [],
    review_status: 'ACTIVE'
  }))
}

function findActiveSegmentIndex(segments, currentMs) {
  if (!Array.isArray(segments) || !segments.length) {
    return -1
  }

  for (let index = 0; index < segments.length; index += 1) {
    const start = Number(segments[index].start_ms || 0)
    const nextStart = index + 1 < segments.length ? Number(segments[index + 1].start_ms || Number.POSITIVE_INFINITY) : Number.POSITIVE_INFINITY

    if (currentMs >= start && currentMs < nextStart) {
      return index
    }
  }

  return currentMs >= Number(segments[segments.length - 1].start_ms || 0) ? segments.length - 1 : -1
}

function findFocusSegmentIndex(segments, focus) {
  if (!Array.isArray(segments) || !segments.length || !focus) {
    return -1
  }

  const activeIndex = findActiveSegmentIndex(segments, Number(focus.seek_ms || 0))
  if (activeIndex >= 0) {
    return activeIndex
  }

  const windowStart = Number(focus.start_ms || focus.seek_ms || 0)
  const normalizedWindowEnd = Number.isFinite(focus.end_ms) ? Number(focus.end_ms) : windowStart
  const windowEnd = Math.max(windowStart + 1, normalizedWindowEnd)

  return segments.findIndex((segment) => segment.end_ms > windowStart && segment.start_ms < windowEnd)
}

function segmentInFocusWindow(segment, focus) {
  if (!segment || !focus) {
    return false
  }

  const windowStart = Number(focus.start_ms || 0)
  const windowEnd = Number.isFinite(focus.end_ms) ? Number(focus.end_ms) : windowStart
  return segment.end_ms > windowStart && segment.start_ms < Math.max(windowStart + 1, windowEnd)
}

function buildClipMap(page) {
  const clipMap = new Map()
  for (const clip of page?.clips || []) {
    clipMap.set(clip.id, clip)
  }
  if (page?.selected_clip?.id) {
    clipMap.set(page.selected_clip.id, {
      ...clipMap.get(page.selected_clip.id),
      ...page.selected_clip
    })
  }
  return clipMap
}

function buildAnnotationMap(page) {
  return new Map((page?.annotations || []).map((annotation) => [annotation.id, annotation]))
}

function deriveTranscriptExcerpt(segments, startMs, endMs, fallback = '') {
  if (!Array.isArray(segments) || !segments.length) {
    return fallback || ''
  }

  const windowStart = Math.max(0, Number(startMs || 0))
  const normalizedEndMs = Number.isFinite(endMs) ? Number(endMs) : windowStart
  const windowEnd = Math.max(windowStart + 1, normalizedEndMs)
  const excerptSegments = segments
    .filter((segment) => segment.end_ms > windowStart && segment.start_ms < windowEnd)
    .slice(0, 4)

  if (!excerptSegments.length) {
    return fallback || ''
  }

  return excerptSegments.map((segment) => segment.text).join('\n')
}

function clipSequenceLabel(index) {
  return `Clip ${index + 1}`
}

function clipWindowLabel(clip) {
  return `${formatClock(clip.start_ms)} - ${formatClock(clip.end_ms)}`
}

function findClipForTime(clips, currentMs) {
  if (!Array.isArray(clips) || !clips.length) {
    return null
  }

  return clips.find((clip) => currentMs >= clip.start_ms && currentMs < clip.end_ms) || null
}

// P4.5 — the ordered clip windows that make up a selected moment's playback.
// Selecting a moment plays ONLY these windows: a single clip stops at its end; a
// multi-clip moment auto-advances clip 1 -> clip 2 -> ... then stops after the
// final clip (skipping any gap between clips). Falls back to the annotation's own
// [start,end] window when it has no published clips.
function buildMomentPlaybackClips(annotation, clipMap) {
  const related = (annotation?.related_clip_ids || [])
    .map((clipId) => clipMap.get(clipId))
    .filter(Boolean)
    .map((clip) => ({
      id: clip.id,
      startMs: Math.max(0, Math.floor(Number(clip.start_ms || 0))),
      endMs: Number.isFinite(Number(clip.end_ms)) ? Math.floor(Number(clip.end_ms)) : null,
    }))
    .filter((clip) => clip.endMs === null || clip.endMs > clip.startMs)
  if (related.length) {
    return related
  }
  const startMs = Math.max(0, Math.floor(Number(annotation?.start_ms || 0)))
  const endMs = Number.isFinite(Number(annotation?.end_ms)) ? Math.floor(Number(annotation.end_ms)) : null
  return endMs !== null && endMs > startMs ? [{ id: annotation?.id || 'moment', startMs, endMs }] : []
}

function transcriptVisibilityPadding(container) {
  return Math.max(32, Math.floor(container.clientHeight * 0.18))
}

function activeRowGeometry(container, row) {
  const containerRect = container.getBoundingClientRect()
  const rowRect = row.getBoundingClientRect()
  const rowTopInContainer = rowRect.top - containerRect.top
  const rowBottomInContainer = rowRect.bottom - containerRect.top

  return {
    rowHeight: rowRect.height,
    rowTopInContainer,
    rowBottomInContainer,
    rowTopInScrollContent: container.scrollTop + rowTopInContainer
  }
}

function rowComfortablyVisible(container, row) {
  const padding = transcriptVisibilityPadding(container)
  const geometry = activeRowGeometry(container, row)

  return geometry.rowTopInContainer >= padding && geometry.rowBottomInContainer <= container.clientHeight - padding
}

function scrollTranscriptRowIntoView(container, row) {
  const padding = transcriptVisibilityPadding(container)
  const geometry = activeRowGeometry(container, row)

  const centeredTop = Math.max(
    0,
    geometry.rowTopInScrollContent - Math.max(padding, Math.floor((container.clientHeight - geometry.rowHeight) / 2))
  )

  container.scrollTo({ top: centeredTop, behavior: 'auto' })
  return centeredTop
}

function ActionNote({ title, text, actionLabel, onAction, className = 'evidence-empty-note' }) {
  return (
    <div className={className}>
      <strong>{title}</strong>
      <p className="muted">{text}</p>
      {actionLabel && typeof onAction === 'function' ? (
        <button type="button" className="ghost" onClick={onAction}>{actionLabel}</button>
      ) : null}
    </div>
  )
}

function SectionHeading({ eyebrow, title, description, className = '' }) {
  const resolvedClassName = ['evidence-section-heading', className].filter(Boolean).join(' ')

  return (
    <div className={resolvedClassName}>
      {eyebrow ? <span className="public-experience-eyebrow">{eyebrow}</span> : null}
      <strong>{title}</strong>
      {description ? <p className="muted">{description}</p> : null}
    </div>
  )
}

function LinkedClipSequence({ clips, activeClipId, onSelect, activeClipLabel = 'Current clip' }) {
  if (!Array.isArray(clips) || clips.length < 1) {
    return null
  }

  return (
    <div className="evidence-sequence-block">
      <div className="evidence-sequence-heading">
        <span className="public-experience-eyebrow">Linked clips</span>
        <p className="muted">Switch between published clip windows for this selected moment.</p>
      </div>
      <div className="evidence-sequence-list" role="list" aria-label="Linked clips">
        {clips.map((clip, index) => {
          const isActive = activeClipId === clip.id

          return (
            <button
              key={clip.id}
              type="button"
              className={`evidence-sequence-chip ${isActive ? 'active' : ''}`.trim()}
              onClick={() => onSelect(clip.id)}
              aria-pressed={isActive}
              aria-label={`${clipSequenceLabel(index)} ${clipWindowLabel(clip)}${isActive ? `, ${activeClipLabel}` : ''}`}
            >
              <strong>{clipSequenceLabel(index)}</strong>
              <span>{clipWindowLabel(clip)}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function DeferredModelCanvasPlaceholder({ eyebrow, title, message, actionLabel = '', onAction, busy = true, highRisk = false, backgroundColor = '#f5efe6' }) {
  const hasAction = Boolean(actionLabel && typeof onAction === 'function')

  return (
    <div className={`model-canvas-shell ${busy ? 'loading' : 'deferred'}${highRisk ? ' high-risk' : ''}`.trim()} aria-busy={busy || undefined} style={{ background: backgroundColor }}>
      <div className="model-canvas-stage model-canvas-stage-placeholder" style={{ background: backgroundColor }} />
      <div
        className={`model-canvas-loading-overlay active ${hasAction ? 'interactive' : ''}`.trim()}
        role={busy ? 'status' : undefined}
        aria-live={busy ? 'polite' : undefined}
        style={{ background: backgroundColor }}
      >
        <div className="model-canvas-loading-copy">
          <div className="model-canvas-loading-mark" aria-hidden="true">
            <BrandMotion name="pivot" size={84} className="brand-motion-glow" />
          </div>
          <div className="model-canvas-loading-text">
            <span className="model-canvas-loading-eyebrow">{eyebrow}</span>
            <strong>{title}</strong>
            <p className={highRisk ? 'model-canvas-loading-warning' : undefined}>{message}</p>
            {busy ? (
              <div className="model-canvas-loading-bar" aria-hidden="true">
                <span />
              </div>
            ) : null}
            {hasAction ? (
              <button type="button" className="model-canvas-load-action" onClick={onAction}>
                {actionLabel}
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}

function ResultThumbnail({ src, alt, fallbackTitle, fallbackNote }) {
  const [failed, setFailed] = useState(false)
  const normalizedSrc = normalizeQueryValue(src)

  if (!normalizedSrc || failed) {
    return (
      <div className="search-result-thumbnail-placeholder media-frame-4x3" role="img" aria-label={fallbackTitle}>
        <strong>{fallbackTitle}</strong>
        <span>{fallbackNote}</span>
      </div>
    )
  }

  return (
    <img
      src={normalizedSrc}
      alt={alt}
      className="search-result-thumbnail media-frame-4x3"
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  )
}

export default function EvidenceObjectApp() {
  const { pathname, search } = readWindowLocation()
  const route = useMemo(() => parseEvidenceRoute(pathname, search), [pathname, search])
  const studioEnabled = useMemo(
    () => isStudioSurfaceEnabled(search, { defaultEnabled: true }),
    [search]
  )
  const handoff = useMemo(() => readEvidenceHandoffState(route.websiteObjectId), [route.websiteObjectId])
  const videoRef = useRef(null)
  const searchInputRef = useRef(null)
  const transcriptListRef = useRef(null)
  const transcriptRowRefs = useRef(new Map())
  const citationFeedbackTimeoutRef = useRef(0)
  const citationDialogRef = useRef(null)
  const citationDialogInitialFocusRef = useRef(null)
  const appliedSeekKeyRef = useRef('')
  const initialMomentPlaybackHandledRef = useRef(false)
  const pendingPlaybackRef = useRef(null)
  const programmaticSeekRef = useRef(false)
  const programmaticSeekTargetMsRef = useRef(null)
  const lastReportedPlaybackMsRef = useRef(0)
  const decodedFrameGenerationRef = useRef(0)
  // P4.5 — guided moment playback: the ordered clip windows to play for the
  // currently selected moment, and the index of the window in progress. `active`
  // is true only while a moment is auto-playing its sequence; a manual scrub or a
  // plain seek disarms it so the full video plays through normally.
  const momentPlaybackRef = useRef({ active: false, clips: [], index: 0 })
  const transcriptScrollSyncTimeoutRef = useRef(0)
  const transcriptProgrammaticScrollRef = useRef(false)
  const evidenceSearchRequestIdRef = useRef(0)
  const transcriptLastSyncedScrollTopRef = useRef(
    Number.isFinite(route.viewState?.tScroll) ? route.viewState.tScroll : 0
  )
  const [isCompactEvidenceLayout, setIsCompactEvidenceLayout] = useState(() => (
    typeof window !== 'undefined' ? window.matchMedia(EVIDENCE_TABLET_MEDIA_QUERY).matches : false
  ))
  const [isMobileEvidenceLayout, setIsMobileEvidenceLayout] = useState(() => (
    typeof window !== 'undefined' ? window.matchMedia(EVIDENCE_MOBILE_MEDIA_QUERY).matches : false
  ))
  const [compactContextView, setCompactContextView] = useState('model')
  const [shouldMountModelCanvas, setShouldMountModelCanvas] = useState(false)
  const [mobileModelLoadRequested, setMobileModelLoadRequested] = useState(false)
  const [page, setPage] = useState(null)
  const [loading, setLoading] = useState(!route.errorState)
  const [errorState, setErrorState] = useState(route.errorState)
  const [currentMs, setCurrentMs] = useState(0)
  // Studio shared-clock duration (ms). Captured from the same <video> element so
  // the linked transport can render a real timeline; legacy path ignores it.
  const [studioDurationMs, setStudioDurationMs] = useState(0)
  const [selectedAnnotationId, setSelectedAnnotationId] = useState('')
  const [modelCameraSelection, setModelCameraSelection] = useState({ annotationId: '', revision: 0 })
  const selectAnnotationWithCamera = useCallback((annotationId, preserveCamera = false) => {
    setSelectedAnnotationId(annotationId)
    setModelCameraSelection((previous) => ({
      annotationId: preserveCamera ? annotationId : '',
      revision: previous.revision + 1,
    }))
  }, [])
  const [activeClipId, setActiveClipId] = useState('')
  const [momentSource, setMomentSource] = useState('')
  const [citationFeedback, setCitationFeedback] = useState('')
  const [citationDialogOpen, setCitationDialogOpen] = useState(false)
  const [citationFormat, setCitationFormat] = useState('apa')
  const [citationAccessDate] = useState(() => formatCitationAccessDate(new Date()))
  const [evidenceSearchQuery, setEvidenceSearchQuery] = useState('')
  const [evidenceSearchSubmittedQuery, setEvidenceSearchSubmittedQuery] = useState('')
  const [evidenceSearchResults, setEvidenceSearchResults] = useState([])
  const [evidenceSearchLoading, setEvidenceSearchLoading] = useState(false)
  const [evidenceSearchError, setEvidenceSearchError] = useState('')
  const [evidenceSearchHasRun, setEvidenceSearchHasRun] = useState(false)
  const [evidenceSearchExpanded, setEvidenceSearchExpanded] = useState(false)
  const [selectedSearchResultId, setSelectedSearchResultId] = useState('')
  const [videoLoadError, setVideoLoadError] = useState('')
  const [transcriptAutoFollow, setTranscriptAutoFollow] = useState(() => !Number.isFinite(route.viewState?.tScroll))
  const [evidenceSearchMeta, setEvidenceSearchMeta] = useState({
    totalResults: 0,
    resultWindow: 15,
    resultWindowCapped: false,
  })
  const requestedTranscriptScrollTop = Number.isFinite(route.viewState?.tScroll)
    ? route.viewState.tScroll
    : null
  const routeHasFocusedMoment = Boolean(route.params.annotation || route.params.clip || route.params.video || route.params.t)
  // P2 (mobile/tablet QA): a plain object URL must land at the TOP. Only auto-
  // scroll to a moment on initial load for an explicit deep link (annotation /
  // clip / video / ?t= / ?t_scroll=). Follow-along during playback is unaffected.
  const autoScrollOnLoad = shouldAutoScrollOnLoad({
    annotation: route.params.annotation,
    clip: route.params.clip,
    video: route.params.video,
    t: route.params.t,
    tScroll: route.viewState?.tScroll,
  })
  const initialAutoScrollHandledRef = useRef(false)
  // Playback key whose deferred media has already been activated, so a second
  // moment click during a long first fetch does not restart it.
  const mediaActivationRef = useRef('')

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined
    }

    const mediaQuery = window.matchMedia(EVIDENCE_TABLET_MEDIA_QUERY)
    const handleViewportChange = (event) => {
      setIsCompactEvidenceLayout(event.matches)
    }

    setIsCompactEvidenceLayout(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleViewportChange)
      return () => mediaQuery.removeEventListener('change', handleViewportChange)
    }

    mediaQuery.addListener(handleViewportChange)
    return () => mediaQuery.removeListener(handleViewportChange)
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined
    }

    const mediaQuery = window.matchMedia(EVIDENCE_MOBILE_MEDIA_QUERY)
    const handleViewportChange = (event) => {
      setIsMobileEvidenceLayout(event.matches)
    }

    setIsMobileEvidenceLayout(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleViewportChange)
      return () => mediaQuery.removeEventListener('change', handleViewportChange)
    }

    mediaQuery.addListener(handleViewportChange)
    return () => mediaQuery.removeListener(handleViewportChange)
  }, [])

  // Adaptive 3D load decision: desktop/laptop auto-loads; mobile / low-memory /
  // Data Saver / constrained-iframe / iOS keep the explicit "Load 3D model"
  // gate. Replaces the prior viewport-width-only gate.
  const modelLoadPolicy = useModelLoadPolicy(page?.object?.id || '')

  // A new object resets the explicit load request so a freshly navigated
  // gated/high-risk object does not auto-mount off the previous tap.
  useEffect(() => {
    setMobileModelLoadRequested(false)
  }, [page?.object?.id])

  useEffect(() => () => {
    if (citationFeedbackTimeoutRef.current && typeof window !== 'undefined') {
      window.clearTimeout(citationFeedbackTimeoutRef.current)
    }
  }, [])

  useEffect(() => {
    if (!citationDialogOpen || typeof document === 'undefined') {
      return undefined
    }

    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // P3: isolate the modal so background controls (incl. the trigger "Copy
    // citation") leave the a11y/focus tree while the dialog is open.
    const restoreBackground = applyModalBackgroundIsolation(citationDialogRef.current)
    window.requestAnimationFrame(() => {
      citationDialogInitialFocusRef.current?.focus()
    })

    return () => {
      document.body.style.overflow = previousOverflow
      restoreBackground()
      // Return focus to the control that opened the dialog.
      if (opener && typeof opener.focus === 'function' && opener.isConnected) {
        opener.focus()
      }
    }
  }, [citationDialogOpen])

  const loadEvidencePage = useCallback(async () => {
    if (route.errorState || !route.websiteObjectId) {
      setLoading(false)
      setErrorState(route.errorState)
      return
    }

    setLoading(true)
    setErrorState(null)

    try {
      const payload = await getPublicEvidencePage(route.websiteObjectId, route.params)
      const initialSeekMs = Number.isFinite(handoff?.seekMs)
        ? handoff.seekMs
        : Number(payload?.playback?.seek_ms || 0)
      const hydrateAnnotationSelection = Boolean(handoff?.annotationId || route.params.annotation || route.params.clip)
      const canonicalUrl = !routeHasFocusedMoment && payload?.focus?.source === 'default'
        ? stripEvidenceFocusParams(payload?.canonical_url)
        : payload?.canonical_url

      lastReportedPlaybackMsRef.current = initialSeekMs
      setPage(payload)
      replaceEvidenceCanonicalUrl(canonicalUrl)
      setCurrentMs(initialSeekMs)
      selectAnnotationWithCamera(hydrateAnnotationSelection ? resolveHydratedAnnotationId(payload, handoff?.annotationId || route.params.annotationContext) : '')
      setActiveClipId(hydrateAnnotationSelection ? payload?.focus?.clip_id || payload?.selected_clip?.id || '' : '')
      setMomentSource(handoff?.source || payload?.focus?.source || 'default')
    } catch (error) {
      setPage(null)
      setErrorState(buildEvidencePageErrorState(error))
    } finally {
      setLoading(false)
    }
  }, [handoff, route.errorState, route.params.annotation, route.params.clip, route.params.t, route.params.video, route.websiteObjectId, routeHasFocusedMoment, selectAnnotationWithCamera])

  useEffect(() => {
    loadEvidencePage()
  }, [loadEvidencePage])

  useEffect(() => {
    setMobileModelLoadRequested(false)
  }, [page?.model?.model_url])

  useEffect(() => {
    if (!isCompactEvidenceLayout) {
      return
    }

    if (Number.isFinite(requestedTranscriptScrollTop)) {
      setCompactContextView('transcript')
      return
    }

    setCompactContextView('model')
  }, [isCompactEvidenceLayout, requestedTranscriptScrollTop])

  useEffect(() => {
    initialAutoScrollHandledRef.current = false
    setTranscriptAutoFollow(!Number.isFinite(requestedTranscriptScrollTop))
  }, [route.requestKey])

  useEffect(() => {
    transcriptLastSyncedScrollTopRef.current = Number.isFinite(requestedTranscriptScrollTop)
      ? requestedTranscriptScrollTop
      : 0
  }, [requestedTranscriptScrollTop])

  useEffect(() => {
    return () => {
      if (transcriptScrollSyncTimeoutRef.current) {
        window.clearTimeout(transcriptScrollSyncTimeoutRef.current)
      }
    }
  }, [])

  const playbackKey = `${page?.playback?.video_stream_url || ''}:${page?.playback?.seek_ms || 0}`

  useEffect(() => {
    appliedSeekKeyRef.current = ''
    pendingPlaybackRef.current = null
    programmaticSeekRef.current = false
    programmaticSeekTargetMsRef.current = null
    initialMomentPlaybackHandledRef.current = false
    lastReportedPlaybackMsRef.current = 0
    setVideoLoadError('')
  }, [playbackKey])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined
    }

    if (!page?.model?.model_url) {
      setShouldMountModelCanvas(false)
      return undefined
    }

    if (modelLoadPolicy.gated) {
      setShouldMountModelCanvas(mobileModelLoadRequested)
      return undefined
    }

    let cancelled = false
    let animationFrameId = 0
    let timeoutId = 0
    let idleCallbackId = 0

    setShouldMountModelCanvas(false)

    const scheduleMount = () => {
      if (cancelled) {
        return
      }
      setShouldMountModelCanvas(true)
    }

    animationFrameId = window.requestAnimationFrame(() => {
      if (typeof window.requestIdleCallback === 'function') {
        idleCallbackId = window.requestIdleCallback(scheduleMount, { timeout: 900 })
        return
      }

      timeoutId = window.setTimeout(scheduleMount, 180)
    })

    return () => {
      cancelled = true
      if (animationFrameId) {
        window.cancelAnimationFrame(animationFrameId)
      }
      if (idleCallbackId && typeof window.cancelIdleCallback === 'function') {
        window.cancelIdleCallback(idleCallbackId)
      }
      if (timeoutId) {
        window.clearTimeout(timeoutId)
      }
    }
  }, [modelLoadPolicy.gated, mobileModelLoadRequested, page?.model?.model_url])

  const scheduleTranscriptScrollUrlSync = useCallback((scrollTop) => {
    if (typeof window === 'undefined') {
      return
    }

    const normalizedScrollTop = normalizeTranscriptScrollTop(scrollTop)
    if (normalizedScrollTop === transcriptLastSyncedScrollTopRef.current) {
      return
    }

    if (transcriptScrollSyncTimeoutRef.current) {
      window.clearTimeout(transcriptScrollSyncTimeoutRef.current)
    }

    transcriptScrollSyncTimeoutRef.current = window.setTimeout(() => {
      transcriptScrollSyncTimeoutRef.current = 0
      replaceEvidenceViewUrl({ transcriptScrollTop: normalizedScrollTop })
      transcriptLastSyncedScrollTopRef.current = normalizedScrollTop
    }, EVIDENCE_TRANSCRIPT_SCROLL_SYNC_DEBOUNCE_MS)
  }, [])

  const applyTranscriptScrollTop = useCallback((container, scrollTop) => {
    if (!(container instanceof HTMLElement)) {
      return
    }

    const normalizedScrollTop = normalizeTranscriptScrollTop(scrollTop)
    transcriptProgrammaticScrollRef.current = true
    container.scrollTo({ top: normalizedScrollTop, behavior: 'auto' })
    scheduleTranscriptScrollUrlSync(normalizedScrollTop)
  }, [scheduleTranscriptScrollUrlSync])

  const handleTranscriptListScroll = useCallback((event) => {
    const nextScrollTop = normalizeTranscriptScrollTop(event.currentTarget.scrollTop)
    const wasProgrammaticScroll = transcriptProgrammaticScrollRef.current
    transcriptProgrammaticScrollRef.current = false

    if (!wasProgrammaticScroll) {
      setTranscriptAutoFollow(false)
    }

    scheduleTranscriptScrollUrlSync(nextScrollTop)
  }, [scheduleTranscriptScrollUrlSync])

  const annotationMap = useMemo(() => buildAnnotationMap(page), [page])
  const clipMap = useMemo(() => buildClipMap(page), [page])

  const selectedAnnotation = useMemo(
    () => (selectedAnnotationId ? annotationMap.get(selectedAnnotationId) || null : null),
    [annotationMap, selectedAnnotationId]
  )

  const defaultSelectedAnnotation = useMemo(() => {
    const defaultAnnotationId = normalizeQueryValue(page?.selected_annotation?.id)
    if (!defaultAnnotationId) {
      return null
    }

    return annotationMap.get(defaultAnnotationId) || page?.selected_annotation || null
  }, [annotationMap, page?.selected_annotation])

  const selectedAnnotationMarkers = useMemo(
    () => annotationsToCanvas(page?.annotations || []),
    [page?.annotations]
  )

  // Studio P3 — unified moment rail source: published annotations become
  // "moments"; clips not already attached to a moment become "auto clips". Sorted
  // by start time so the rail reads left-to-right along the shared clock.
  const studioRailItems = useMemo(() => {
    const annotations = page?.annotations || []
    const clips = page?.clips || []
    const referencedClipIds = new Set()
    // First related clip's poster (curated moment) / the clip's own poster (auto
    // clip). Prefer an explicit poster_url; else derive the public clip-poster
    // endpoint from the clip id. Resolved to an absolute URL for the API host.
    const posterFor = (clip) => {
      if (!clip?.id) {
        return ''
      }
      const path = clip.poster_url || `/api/public/clips/${encodeURIComponent(clip.id)}/poster`
      return resolveApiUrl(path) || ''
    }
    const momentItems = annotations.map((annotation) => {
      const related = Array.isArray(annotation.related_clip_ids) ? annotation.related_clip_ids : []
      related.forEach((clipId) => referencedClipIds.add(clipId))
      const firstClip = related.map((clipId) => clipMap.get(clipId)).find(Boolean) || null
      const startMs = Number(firstClip?.start_ms ?? annotation.start_ms ?? 0)
      const endMs = Number(firstClip?.end_ms ?? annotation.end_ms ?? startMs)
      return {
        id: `moment:${annotation.id}`,
        kind: 'moment',
        annotationId: annotation.id,
        clipId: firstClip?.id || '',
        posterUrl: posterFor(firstClip),
        label: annotation.title || 'Moment',
        startMs,
        endMs,
      }
    })
    const clipItems = clips
      .filter((clip) => !referencedClipIds.has(clip.id))
      .map((clip, index) => ({
        id: `clip:${clip.id}`,
        kind: 'clip',
        clipId: clip.id,
        posterUrl: posterFor(clip),
        label: clip.title || `Auto clip ${index + 1}`,
        startMs: Number(clip.start_ms || 0),
        endMs: Number(clip.end_ms ?? clip.start_ms ?? 0),
      }))
    return [...momentItems, ...clipItems].sort((a, b) => a.startMs - b.startMs)
  }, [page?.annotations, page?.clips, clipMap])

  // Moment pips for the linked transport (curated moments only, not auto clips).
  const studioMomentMarkers = useMemo(
    () => studioRailItems.filter((item) => item.kind === 'moment'),
    [studioRailItems]
  )

  // The moment currently being watched, for the contextual interaction cue. Null
  // when no moment is selected (the cue then shows only its discoverability line).
  const studioActiveMoment = useMemo(() => {
    if (!selectedAnnotation) {
      return null
    }
    const marker = studioRailItems.find((item) => item.annotationId === selectedAnnotation.id)
    return { title: selectedAnnotation.title, startMs: Number(marker?.startMs ?? selectedAnnotation.start_ms ?? 0) }
  }, [selectedAnnotation, studioRailItems])

  // Navigation frames this moment; direct canvas selection retains the live
  // camera through its separate selection intent. Idle framing defaults null.
  const studioFocusAnnotation = useMemo(
    () => (selectedAnnotationId
      ? selectedAnnotationMarkers.find((marker) => marker.id === selectedAnnotationId) || null
      : null),
    [selectedAnnotationId, selectedAnnotationMarkers]
  )

  const relatedClipSequence = useMemo(
    () => (selectedAnnotation?.related_clip_ids || []).map((clipId) => clipMap.get(clipId)).filter(Boolean),
    [clipMap, selectedAnnotation]
  )

  // Studio P4.1 — clip windows for the selected moment (restores the published
  // multi-clip switcher inside the Studio). Empty when no moment is selected.
  const studioClipSequence = useMemo(
    () => relatedClipSequence.map((clip, index) => ({
      id: clip.id,
      label: `Clip ${index + 1}`,
      startMs: Number(clip.start_ms || 0),
      endMs: Number(clip.end_ms ?? clip.start_ms ?? 0),
    })),
    [relatedClipSequence]
  )

  const visibleClipSequence = useMemo(() => {
    const sequenceSource = selectedAnnotation || defaultSelectedAnnotation
    return (sequenceSource?.related_clip_ids || []).map((clipId) => clipMap.get(clipId)).filter(Boolean)
  }, [clipMap, defaultSelectedAnnotation, selectedAnnotation])

  const activeClip = useMemo(
    () => (activeClipId ? clipMap.get(activeClipId) || null : null),
    [activeClipId, clipMap]
  )

  const activeSearchResult = useMemo(
    () => evidenceSearchResults.find((result) => result.segment_id === selectedSearchResultId) || null,
    [evidenceSearchResults, selectedSearchResultId]
  )

  const activeSearchResultFocus = useMemo(
    () => (activeSearchResult ? surroundingContextRange(activeSearchResult) : null),
    [activeSearchResult]
  )

  const playbackSegmentIndex = useMemo(
    () => findActiveSegmentIndex(page?.transcript?.segments || [], currentMs),
    [currentMs, page?.transcript?.segments]
  )

  const playbackSegment = playbackSegmentIndex >= 0
    ? page?.transcript?.segments?.[playbackSegmentIndex] || null
    : null

  const playbackMatchedClip = useMemo(
    () => findClipForTime(relatedClipSequence, currentMs),
    [currentMs, relatedClipSequence]
  )

  const resolvedActiveClip = playbackMatchedClip || activeClip
  const visibleSequenceActiveClipId = resolvedActiveClip?.id
    || normalizeQueryValue(page?.focus?.clip_id)
    || normalizeQueryValue(page?.selected_clip?.id)
    || (visibleClipSequence.length === 1 ? visibleClipSequence[0]?.id || '' : '')

  useEffect(() => {
    if (!playbackMatchedClip || playbackMatchedClip.id === activeClipId) {
      return
    }

    setActiveClipId(playbackMatchedClip.id)
  }, [activeClipId, playbackMatchedClip])

  const selectedMomentSource = normalizeQueryValue(momentSource || page?.focus?.source || 'default') || 'default'
  const activeFocus = useMemo(() => {
    const pageFocus = page?.focus || {}
    const source = activeSearchResultFocus ? 'video' : selectedMomentSource
    const videoId = normalizeQueryValue(page?.playback?.video_id || pageFocus.video_id)
    const fallbackSeekMs = Math.max(0, Math.floor(Number(pageFocus.seek_ms ?? pageFocus.start_ms ?? 0)))
    const fallbackStartMs = Math.max(0, Math.floor(Number(pageFocus.start_ms ?? fallbackSeekMs)))
    const fallbackEndMs = Number.isFinite(Number(pageFocus.end_ms))
      ? Math.max(fallbackStartMs, Math.floor(Number(pageFocus.end_ms)))
      : null

    let annotationId = null
    let clipId = null
    let seekMs = fallbackSeekMs
    let startMs = fallbackStartMs
    let endMs = fallbackEndMs

    if (activeSearchResultFocus) {
      seekMs = activeSearchResultFocus.momentStart
      startMs = activeSearchResultFocus.momentStart
      endMs = activeSearchResultFocus.momentEnd
    } else if (source === 'video') {
      startMs = Math.max(0, Math.floor(Number(playbackSegment?.start_ms ?? currentMs ?? fallbackStartMs)))
      seekMs = Math.max(0, Math.floor(Number(currentMs ?? fallbackSeekMs)))
      endMs = Number.isFinite(Number(playbackSegment?.end_ms))
        ? Math.max(startMs, Math.floor(Number(playbackSegment.end_ms)))
        : null
    } else if (source === 'clip') {
      const clip = resolvedActiveClip || page?.selected_clip || null
      annotationId = selectedAnnotation?.id || null
      clipId = normalizeQueryValue(clip?.id)
      startMs = Math.max(0, Math.floor(Number(clip?.start_ms ?? fallbackStartMs)))
      seekMs = startMs
      endMs = Number.isFinite(Number(clip?.end_ms))
        ? Math.max(startMs, Math.floor(Number(clip.end_ms)))
        : null
    } else if (source === 'annotation') {
      const clip = resolvedActiveClip || null
      annotationId = selectedAnnotation?.id || normalizeQueryValue(pageFocus.annotation_id)
      clipId = normalizeQueryValue(clip?.id)
      startMs = Math.max(0, Math.floor(Number(clip?.start_ms ?? selectedAnnotation?.start_ms ?? fallbackStartMs)))
      seekMs = startMs
      endMs = Number.isFinite(Number(clip?.end_ms))
        ? Math.max(startMs, Math.floor(Number(clip.end_ms)))
        : Number.isFinite(Number(selectedAnnotation?.end_ms))
          ? Math.max(startMs, Math.floor(Number(selectedAnnotation.end_ms)))
          : fallbackEndMs
    } else if (source === 't') {
      annotationId = null
      clipId = null
      startMs = fallbackStartMs
      seekMs = fallbackSeekMs
      endMs = fallbackEndMs
    } else {
      annotationId = null
      clipId = null
    }

    return {
      ...pageFocus,
      source,
      annotation_id: annotationId || null,
      clip_id: clipId || null,
      video_id: videoId,
      seek_ms: seekMs,
      start_ms: startMs,
      end_ms: endMs
    }
  }, [activeSearchResultFocus, currentMs, page?.focus, page?.playback?.video_id, page?.selected_clip, playbackSegment, resolvedActiveClip, selectedAnnotation, selectedMomentSource])

  const activeCitationFocus = activeFocus
  const focusStartMs = activeFocus.start_ms ?? 0
  const focusEndMs = activeFocus.end_ms ?? null
  const focusSeekMs = activeFocus.seek_ms ?? focusStartMs

  const defaultEvidenceLanding = Boolean(
    !routeHasFocusedMoment
    && !handoff?.annotationId
    && !selectedAnnotationId
    && !activeClipId
    && !selectedSearchResultId
  )
  const videoMomentTitle = ['video', 't'].includes(activeFocus?.source)
    ? `Transcript ${formatFocusWindow(activeFocus)}`
    : ''
  const currentMomentTitle = useMemo(() => {
    if (activeSearchResultFocus) {
      return `Search result ${formatClock(activeSearchResultFocus.momentStart)} - ${formatClock(activeSearchResultFocus.momentEnd)}`
    }

    if (activeFocus.source === 'clip') {
      return resolvedActiveClip?.title || page?.selected_clip?.title || selectedAnnotation?.title || page?.page?.title || page?.object?.title || ''
    }

    if (activeFocus.source === 'annotation') {
      return selectedAnnotation?.title || page?.selected_annotation?.title || resolvedActiveClip?.title || page?.page?.title || page?.object?.title || ''
    }

    if (activeFocus.source === 'video' || activeFocus.source === 't') {
      return videoMomentTitle || page?.page?.title || page?.object?.title || ''
    }

    return (defaultEvidenceLanding ? page?.object?.title : page?.page?.title) || page?.object?.title || ''
  }, [activeFocus.source, activeSearchResultFocus, defaultEvidenceLanding, page?.object?.title, page?.page?.title, page?.selected_annotation?.title, page?.selected_clip?.title, resolvedActiveClip?.title, selectedAnnotation?.title, videoMomentTitle])
  const currentMomentSummary = useMemo(() => {
    if (activeSearchResult) {
      return normalizeQueryValue(activeSearchResult.scene_description || activeSearchResult.text)
    }

    if (activeFocus.source === 'clip') {
      return selectedAnnotation?.description || resolvedActiveClip?.transcript_excerpt || page?.selected_clip?.transcript_excerpt || page?.page?.summary || ''
    }

    if (activeFocus.source === 'annotation') {
      return selectedAnnotation?.description || page?.selected_annotation?.description || page?.page?.summary || ''
    }

    return (defaultEvidenceLanding ? page?.object?.summary : page?.page?.summary) || ''
  }, [activeFocus.source, activeSearchResult, defaultEvidenceLanding, page?.object?.summary, page?.page?.summary, page?.selected_annotation?.description, page?.selected_clip?.transcript_excerpt, resolvedActiveClip?.transcript_excerpt, selectedAnnotation?.description])
  const evidenceMetaImage = useMemo(() => {
    const posterPath = normalizeQueryValue(page?.object?.poster_url || '')
    return posterPath ? resolvePublicObjectPosterUrl(posterPath) : ''
  }, [page?.object?.poster_url])

  useEffect(() => {
    let cancelled = false
    let timeoutId = 0
    let idleCallbackId = 0

    const objectName = normalizeQueryValue(page?.object?.name || page?.object?.title || route.websiteObjectId)
    const title = objectName ? `${objectName} Evidence` : 'Evidence'
    const description = normalizeQueryValue(currentMomentSummary)
      || (objectName
        ? `Explore video evidence, transcript context, and located moments for ${objectName}.`
        : 'Explore video evidence, transcript context, and located moments.')

    const applyPageMeta = async () => {
      const { setPageMeta } = await import('../lib/seo')
      if (cancelled) {
        return
      }

      setPageMeta({
        title,
        description,
        image: evidenceMetaImage,
        url: window.location.href,
        updateDocumentTitle: true,
      })
    }

    if (typeof window.requestIdleCallback === 'function') {
      idleCallbackId = window.requestIdleCallback(() => {
        void applyPageMeta()
      }, { timeout: 1200 })
    } else {
      timeoutId = window.setTimeout(() => {
        void applyPageMeta()
      }, 120)
    }

    return () => {
      cancelled = true
      if (idleCallbackId && typeof window.cancelIdleCallback === 'function') {
        window.cancelIdleCallback(idleCallbackId)
      }
      if (timeoutId) {
        window.clearTimeout(timeoutId)
      }
    }
  }, [currentMomentSummary, evidenceMetaImage, page?.object?.name, page?.object?.title, route.websiteObjectId])

  const excerpt = useMemo(
    () => deriveTranscriptExcerpt(
      page?.transcript?.segments || [],
      focusStartMs,
      focusEndMs,
      resolvedActiveClip?.id === page?.selected_clip?.id ? page?.selected_clip?.transcript_excerpt || '' : ''
    ),
    [focusEndMs, focusStartMs, page?.selected_clip?.id, page?.selected_clip?.transcript_excerpt, page?.transcript?.segments, resolvedActiveClip?.id]
  )
  const activeCitationSource = activeCitationFocus?.source
  const hasCitationMomentOverride = Boolean(['annotation', 'clip', 'video', 't'].includes(activeCitationSource))
  const citationObjectTitle = collapseWhitespace(page?.object?.title || '')
  const citationCollectionName = collapseWhitespace(page?.object?.project_name || page?.object?.project_slug || '')
  const citationFocusType = focusLabel(activeCitationSource)
  const citationTimeLabel = formatFocusWindow(activeCitationFocus)
  const citationMomentTitle = useMemo(() => collapseWhitespace(currentMomentTitle), [currentMomentTitle])
  const citationExcerpt = useMemo(
    () => truncateText(String(excerpt || '').replace(/"/g, "'")),
    [excerpt]
  )
  const objectCanonicalUrl = useMemo(() => {
    const strippedCanonicalUrl = stripEvidenceFocusParams(page?.canonical_url || '')
    const fallbackObjectUrl = route.websiteObjectId
      ? `/evidence/objects/${encodeURIComponent(route.websiteObjectId)}`
      : ''
    return resolveAbsolutePublicUrl(strippedCanonicalUrl || fallbackObjectUrl || page?.canonical_url || '')
  }, [page?.canonical_url, route.websiteObjectId])
  const citationCanonicalUrl = useMemo(() => {
    if (!page?.canonical_url && !objectCanonicalUrl) {
      return ''
    }

    if (!hasCitationMomentOverride) {
      return objectCanonicalUrl
    }

    return buildCanonicalEvidenceMomentUrl({
      canonicalUrl: page.canonical_url,
      websiteObjectId: route.websiteObjectId,
      source: activeCitationSource,
      annotationId: activeCitationFocus?.annotation_id,
      clipId: activeCitationFocus?.clip_id,
      videoId: activeCitationFocus?.video_id || page?.playback?.video_id,
      seekMs: activeCitationFocus?.seek_ms
    })
  }, [
    activeCitationFocus?.annotation_id,
    activeCitationFocus?.clip_id,
    activeCitationFocus?.seek_ms,
    activeCitationFocus?.video_id,
    activeCitationSource,
    hasCitationMomentOverride,
    objectCanonicalUrl,
    page?.canonical_url,
    page?.playback?.video_id,
    route.websiteObjectId,
  ])
  // P4.6 — the focus used ONLY for SPEAKER attribution. It follows the live
  // playback clock so the resolved speaker tracks whoever is speaking at the
  // current moment — during free playback AND within a selected clip. The
  // citation URL/label keep using `activeCitationFocus` (stable annotation/clip
  // anchors); only the attribution seek time is dynamic. Always carries
  // video_id + seek_ms/start_ms/end_ms so selectCitationAttributionForFocus can
  // resolve the timeline.
  const citationAttributionFocus = useMemo(() => {
    const videoId = normalizeQueryValue(activeCitationFocus?.video_id || page?.playback?.video_id)
    const liveMs = Math.max(0, Math.floor(Number(currentMs) || 0))
    const windowStart = Number.isFinite(Number(activeCitationFocus?.start_ms))
      ? Math.max(0, Math.floor(Number(activeCitationFocus.start_ms)))
      : null
    const windowEnd = Number.isFinite(Number(activeCitationFocus?.end_ms))
      ? Math.max(0, Math.floor(Number(activeCitationFocus.end_ms)))
      : null
    const stableSeek = Number.isFinite(Number(activeCitationFocus?.seek_ms))
      ? Math.max(0, Math.floor(Number(activeCitationFocus.seek_ms)))
      : (windowStart ?? 0)
    // Prefer the live clock once playback has advanced; fall back to the stable
    // focus seek before playback starts so the speaker still resolves on load.
    const seekMs = liveMs > 0 ? liveMs : stableSeek
    return {
      video_id: videoId,
      seek_ms: seekMs,
      start_ms: windowStart ?? seekMs,
      end_ms: windowEnd,
    }
  }, [
    activeCitationFocus?.video_id,
    activeCitationFocus?.seek_ms,
    activeCitationFocus?.start_ms,
    activeCitationFocus?.end_ms,
    currentMs,
    page?.playback?.video_id,
  ])
  const rawCitationAttribution = useMemo(
    () => selectCitationAttributionForFocus(
      page?.citation_attribution_timeline || page?.citationAttributionTimeline || [],
      citationAttributionFocus,
      page?.citation_attribution || page?.citationAttribution || null
    ),
    [citationAttributionFocus, page?.citation_attribution, page?.citation_attribution_timeline, page?.citationAttribution, page?.citationAttributionTimeline]
  )
  const citationAttribution = useMemo(
    () => normalizeCitationAttribution(rawCitationAttribution),
    [rawCitationAttribution]
  )
  const citationPlainText = useMemo(
    () => buildPlainTextEvidenceCitation({
      objectTitle: citationObjectTitle,
      momentTitle: citationMomentTitle,
      collectionName: citationCollectionName,
      speakerLabel: citationAttribution.speakerLabel,
      recordedDate: citationAttribution.recordedDateLabel,
      focusType: citationFocusType,
      timeLabel: citationTimeLabel,
      excerpt: citationExcerpt,
      canonicalUrl: citationCanonicalUrl,
      accessDate: citationAccessDate
    }),
    [citationAccessDate, citationAttribution.recordedDateLabel, citationAttribution.speakerLabel, citationCanonicalUrl, citationCollectionName, citationExcerpt, citationFocusType, citationMomentTitle, citationObjectTitle, citationTimeLabel]
  )
  const formattedCitationText = useMemo(
    () => buildFormattedEvidenceCitation(citationFormat, {
      objectTitle: citationObjectTitle,
      momentTitle: citationMomentTitle,
      collectionName: citationCollectionName,
      speakerLabel: citationAttribution.speakerLabel,
      speakerAuthorName: citationAttribution.speakerAuthorName,
      recordedDate: citationAttribution.recordedDateLabel,
      apaRecordedDate: citationAttribution.apaDateLabel,
      focusType: citationFocusType,
      timeLabel: citationTimeLabel,
      excerpt: citationExcerpt,
      canonicalUrl: citationCanonicalUrl,
      accessDate: citationAccessDate
    }),
    [citationAccessDate, citationAttribution.apaDateLabel, citationAttribution.recordedDateLabel, citationAttribution.speakerAuthorName, citationAttribution.speakerLabel, citationCanonicalUrl, citationCollectionName, citationExcerpt, citationFocusType, citationFormat, citationMomentTitle, citationObjectTitle, citationTimeLabel]
  )
  const selectedCitationFormatLabel = CITATION_FORMAT_OPTIONS.find((option) => option.id === citationFormat)?.label || 'Citation'
  const citationMetaRows = useMemo(() => {
    const rows = [
      { label: 'Object', value: citationObjectTitle },
      { label: 'Speaker', value: citationAttribution.detailSpeakerLabel },
      { label: 'Recorded', value: citationAttribution.detailRecordedLabel },
      { label: 'Collection', value: citationCollectionName },
      { label: 'Focus', value: citationFocusType },
      { label: 'Time', value: citationTimeLabel },
      { label: 'Accessed', value: citationAccessDate }
    ]

    if (citationMomentTitle && citationMomentTitle !== citationObjectTitle) {
      rows.splice(1, 0, { label: 'Moment', value: citationMomentTitle })
    }

    return rows.filter((row) => row.value)
  }, [citationAccessDate, citationAttribution.detailRecordedLabel, citationAttribution.detailSpeakerLabel, citationCollectionName, citationFocusType, citationMomentTitle, citationObjectTitle, citationTimeLabel])

  const setCitationFeedbackMessage = useCallback((message) => {
    if (citationFeedbackTimeoutRef.current && typeof window !== 'undefined') {
      window.clearTimeout(citationFeedbackTimeoutRef.current)
    }

    setCitationFeedback(message)
    if (!message || typeof window === 'undefined') {
      citationFeedbackTimeoutRef.current = 0
      return
    }

    citationFeedbackTimeoutRef.current = window.setTimeout(() => {
      citationFeedbackTimeoutRef.current = 0
      setCitationFeedback('')
    }, 2400)
  }, [])

  const handleCitationCopy = useCallback(async (kind) => {
    const payload = kind === 'link'
      ? citationCanonicalUrl
      : kind === 'plain'
        ? formattedCitationText || citationPlainText
        : citationPlainText

    if (!payload) {
      setCitationFeedbackMessage('Copy failed. Citation details are not available for this moment.')
      return
    }

    const copied = await copyTextToClipboard(payload)
    setCitationFeedbackMessage(
      copied
        ? kind === 'link'
          ? 'Link copied.'
          : 'Citation copied.'
        : 'Copy failed. Browser access to the clipboard is unavailable.'
    )
  }, [citationCanonicalUrl, citationPlainText, formattedCitationText, setCitationFeedbackMessage])

  const openCitationDialog = useCallback(() => {
    if (!citationPlainText) {
      setCitationFeedbackMessage('Citation details are not available for this moment.')
      return
    }

    setCitationDialogOpen(true)
  }, [citationPlainText, setCitationFeedbackMessage])

  const handleCitationDialogKeyDown = useCallback((event) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      setCitationDialogOpen(false)
      return
    }

    if (event.key !== 'Tab') {
      return
    }

    const dialog = citationDialogRef.current
    if (!dialog) {
      return
    }

    const focusableElements = Array.from(dialog.querySelectorAll(
      'a[href], button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )).filter((element) => element instanceof HTMLElement && element.offsetParent !== null)
    if (!focusableElements.length) {
      return
    }

    const first = focusableElements[0]
    const last = focusableElements[focusableElements.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }, [])

  const explicitMomentEntry = Boolean(route.params.annotation || route.params.clip)
  const searchVideoId = useMemo(
    () => publicVideoIdFromStreamUrl(page?.playback?.video_stream_url || ''),
    [page?.playback?.video_stream_url]
  )
  const showEvidenceSearch = Boolean(!explicitMomentEntry && searchVideoId && page?.transcript?.segments?.length)
  const evidenceSearchHasResults = evidenceSearchResults.length > 0
  const evidenceSearchSummary = evidenceSearchLoading
    ? 'Searching this recording'
    : evidenceSearchMeta.totalResults
      ? evidenceSearchMeta.resultWindowCapped
        ? `Showing ${evidenceSearchResults.length} of top ${evidenceSearchMeta.resultWindow} ranked matches in this recording`
        : `Showing ${evidenceSearchResults.length} of ${evidenceSearchMeta.totalResults} matches in this recording`
      : evidenceSearchHasRun
        ? 'No transcript lines match this search'
        : ''
  const evidenceSearchStatusNote = evidenceSearchLoading
    ? 'Ranking transcript lines and preparing matching moments.'
    : activeSearchResult
      ? `Last jump ${formatClock(activeSearchResult.start_ms)} - ${formatClock(activeSearchResult.end_ms)} is now active in the player and transcript.`
      : evidenceSearchHasResults
        ? 'Choose a line to move the main evidence view without leaving this page.'
        : evidenceSearchHasRun
          ? 'Try another word or phrase from this recording.'
          : 'Use a word or phrase from the transcript to explore related moments without leaving this page.'
  const hasActiveEvidenceSearchState = Boolean(
    evidenceSearchHasRun
    || evidenceSearchSubmittedQuery
    || evidenceSearchResults.length
    || selectedSearchResultId
    || evidenceSearchError
    || evidenceSearchLoading
  )

  useEffect(() => {
    evidenceSearchRequestIdRef.current += 1
    setEvidenceSearchQuery('')
    setEvidenceSearchSubmittedQuery('')
    setEvidenceSearchResults([])
    setEvidenceSearchLoading(false)
    setEvidenceSearchError('')
    setEvidenceSearchHasRun(false)
    setEvidenceSearchExpanded(false)
    setSelectedSearchResultId('')
    setEvidenceSearchMeta({
      totalResults: 0,
      resultWindow: 15,
      resultWindowCapped: false,
    })
  }, [searchVideoId])

  const initialPlaybackRequest = useMemo(() => {
    if (!page?.playback?.video_stream_url) {
      return null
    }

    if (handoff) {
      return {
        seekMs: Number.isFinite(handoff.seekMs) ? handoff.seekMs : Math.max(0, Number(focusSeekMs || 0)),
        autoplay: false
      }
    }

    if (!routeHasFocusedMoment) {
      return null
    }

    return {
      seekMs: Math.max(0, Number(focusSeekMs || 0)),
      autoplay: false
    }
  }, [focusSeekMs, handoff, page?.playback?.video_stream_url, routeHasFocusedMoment])

  function playbackRequestSettled(target, request) {
    if (!target || !request) {
      return false
    }

    const nextTime = Math.max(0, Number(request.seekMs || 0) / 1000)
    if (!Number.isFinite(nextTime)) {
      return false
    }

    return Math.abs(Number(target.currentTime || 0) - nextTime) <= 0.35
  }

  function commitPlaybackRequest(target, request, { allowAutoplay = false } = {}) {
    if (!target || !request) {
      return false
    }

    const nextTime = Math.max(0, Number(request.seekMs || 0) / 1000)
    if (!Number.isFinite(nextTime)) {
      return false
    }

    if (!playbackRequestSettled(target, request)) {
      try {
        programmaticSeekRef.current = true
        programmaticSeekTargetMsRef.current = Math.round(nextTime * 1000)
        target.currentTime = nextTime
        decodedFrameGenerationRef.current += 1
      } catch {
        programmaticSeekRef.current = false
        programmaticSeekTargetMsRef.current = null
        return false
      }
    }

    if (!playbackRequestSettled(target, request)) {
      return false
    }

  lastReportedPlaybackMsRef.current = Math.round(nextTime * 1000)
    setCurrentMs(Math.round(nextTime * 1000))

    if (request.autoplay && allowAutoplay) {
      const playResult = target.play?.()
      if (playResult && typeof playResult.catch === 'function') {
        playResult.catch(() => {})
      }
    }

    return !request.autoplay || allowAutoplay
  }

  function focusVideoMoment(seekMs, autoplay = false) {
    const request = { seekMs, autoplay }
    pendingPlaybackRef.current = request
    const target = videoRef.current

    if (!target) {
      return
    }

    if (target.readyState < 1) {
      // Plain object routes intentionally defer the large session video. A
      // direct user action starts the existing same-origin media element and
      // leaves the pending seek for onLoadedMetadata/onCanPlay to settle.
      //
      // `load()` aborts any fetch already in flight and resets the element, so
      // firing it again while the first activation is still downloading would
      // discard the bytes already received and restart from zero. Since
      // readyState stays 0 for the whole first stretch of a 219 MB fetch, a
      // visitor clicking a second moment during startup would otherwise do
      // exactly that. Activate at most once per playback source; the newer
      // pending request above still wins when metadata arrives.
      if (mediaActivationRef.current !== playbackKey) {
        mediaActivationRef.current = playbackKey
        try {
          target.load()
        } catch {
          return
        }
      }
      if (autoplay && typeof target.play === 'function') {
        const playResult = target.play()
        if (playResult && typeof playResult.catch === 'function') {
          playResult.catch(() => {})
        }
      }
      return
    }

    const requestComplete = commitPlaybackRequest(target, request, {
      allowAutoplay: autoplay && target.readyState >= 2 && !target.seeking
    })
    if (!requestComplete) {
      return
    }

    programmaticSeekRef.current = false
    pendingPlaybackRef.current = null
    appliedSeekKeyRef.current = playbackKey
  }

  // P4.5 — arm guided moment playback (Studio only) for an ordered set of clip
  // windows, starting at `startIndex`. handleVideoTimeUpdate enforces the window
  // boundaries: single clip stops at its end; multi-clip auto-advances then stops.
  const armMomentPlayback = useCallback((clips, startIndex = 0) => {
    if (!studioEnabled || !Array.isArray(clips) || clips.length === 0) {
      momentPlaybackRef.current = { active: false, clips: [], index: 0 }
      return
    }
    const index = Math.min(Math.max(0, startIndex), clips.length - 1)
    momentPlaybackRef.current = { active: true, clips, index, lastFrameMs: clips[index].startMs, frameStepMs: 1000 / 30 }
  }, [studioEnabled])

  const disarmMomentPlayback = useCallback(() => {
    momentPlaybackRef.current = { active: false, clips: [], index: 0 }
  }, [])

  // Single Studio seek primitive. `target` is the shared seek shape that later
  // slices (moment rail, search jump, auto-focus) will extend with annotation /
  // clip ids; for this slice it carries { seekMs, source, autoplay }. It resets
  // the moment selection to a plain timestamp focus and never widens the public
  // payload — it only drives the existing video clock.
  function seekStudioMoment(target) {
    const seekMs = Math.max(0, Math.floor(Number(target?.seekMs) || 0))
    // A transcript selection always seeks the spoken line. Retain any spatial
    // annotation focus separately from its guided clip playback, whose start
    // can precede this line. Citation timing follows the shared video clock.
    const moment = target?.source === 'transcript'
      ? studioRailItems.find(
        (item) => item.kind === 'moment'
          && seekMs >= item.startMs
          && seekMs < Math.max(item.startMs + 1, item.endMs)
      )
      : null
    disarmMomentPlayback()
    setTranscriptAutoFollow(true)
    setSelectedSearchResultId('')
    selectAnnotationWithCamera(moment?.annotationId || '')
    setActiveClipId('')
    setMomentSource('video')
    setCurrentMs(seekMs)
    focusVideoMoment(seekMs, Boolean(target?.autoplay))
  }

  const handleVideoTimeUpdate = useCallback((event, decodedMs = null) => {
    const media = event.currentTarget
    const mediaDuration = Number(media.duration)
    if (Number.isFinite(mediaDuration) && mediaDuration > 0) {
      const nextDurationMs = Math.floor(mediaDuration * 1000)
      setStudioDurationMs((current) => (current === nextDurationMs ? current : nextDurationMs))
    }
    const nextMs = Math.max(0, decodedMs ?? Math.floor(event.currentTarget.currentTime * 1000))

    // P4.5 — enforce guided moment playback boundaries. Checked BEFORE the render
    // throttle so a clip end is never skipped. On reaching the current window end:
    // advance to the next clip's start (auto-advance, skipping any gap) or, if this
    // was the final clip, pause and disarm so a later manual play continues the
    // full video. Bounded O(1) ref work — no DOM scans.
    const guided = momentPlaybackRef.current
    if (guided.active && guided.clips.length) {
      const boundary = guidedWindowBoundary(guided, nextMs, mediaDuration * 1000, decodedMs !== null)
      if (boundary.finish) {
        if (guided.index + 1 < guided.clips.length) {
          const nextClip = guided.clips[guided.index + 1]
          guided.index += 1
          guided.hasDecodedFrame = false
          guided.lastFrameMs = nextClip.startMs
          guided.frameStepMs = 1000 / 30
          programmaticSeekRef.current = true
          programmaticSeekTargetMsRef.current = nextClip.startMs
          try {
            media.currentTime = nextClip.startMs / 1000
            decodedFrameGenerationRef.current += 1
          } catch {
            programmaticSeekRef.current = false
            programmaticSeekTargetMsRef.current = null
          }
          lastReportedPlaybackMsRef.current = nextClip.startMs
          setCurrentMs(nextClip.startMs)
          if (nextClip.id) {
            setActiveClipId(nextClip.id)
          }
          return
        }
        momentPlaybackRef.current = { active: false, clips: [], index: 0 }
        programmaticSeekRef.current = true
        programmaticSeekTargetMsRef.current = boundary.stopMs
        try {
          media.pause()
          media.currentTime = boundary.stopMs / 1000
          decodedFrameGenerationRef.current += 1
        } catch {
          programmaticSeekRef.current = false
          programmaticSeekTargetMsRef.current = null
        }
        lastReportedPlaybackMsRef.current = boundary.stopMs
        setCurrentMs(boundary.stopMs)
        return
      }
    }

    if (
      nextMs !== 0
      && Math.abs(nextMs - lastReportedPlaybackMsRef.current) < EVIDENCE_PLAYBACK_RENDER_INTERVAL_MS
    ) {
      return
    }

    lastReportedPlaybackMsRef.current = nextMs
    setCurrentMs(nextMs)
    if (
      activeSearchResultFocus
      && (nextMs < activeSearchResultFocus.momentStart || nextMs >= activeSearchResultFocus.momentEnd)
    ) {
      setSelectedSearchResultId('')
    }
  }, [activeSearchResultFocus])

  // Decoded frame callbacks keep short guided windows independent of the
  // browser's coarse timeupdate cadence. The fallback watches the media clock.
  useEffect(() => {
    const media = videoRef.current
    if (!media) return undefined
    let frame = 0
    let disposed = false
    const hasVideoFrames = typeof media.requestVideoFrameCallback === 'function'
    const schedule = () => {
      if (disposed || media.paused || media.ended || media.seeking) return
      const generation = decodedFrameGenerationRef.current
      frame = hasVideoFrames
        ? media.requestVideoFrameCallback((now, metadata) => tick(generation, now, metadata))
        : window.requestAnimationFrame((now) => tick(generation, now, null))
    }
    const tick = (generation, _now, metadata) => {
      if (disposed || generation !== decodedFrameGenerationRef.current || media.seeking || media.paused) return
      const decodedMs = hasVideoFrames ? Number(metadata.mediaTime) * 1000 : null
      if (hasVideoFrames && !Number.isFinite(decodedMs)) return
      handleVideoTimeUpdate({ currentTarget: media }, decodedMs)
      schedule()
    }
    const cancel = () => {
      if (hasVideoFrames) media.cancelVideoFrameCallback(frame)
      else window.cancelAnimationFrame(frame)
    }
    const onSeeking = () => {
      decodedFrameGenerationRef.current += 1
      cancel()
    }
    const onSeeked = () => { schedule() }
    const start = () => { cancel(); schedule() }
    media.addEventListener('play', start)
    media.addEventListener('pause', cancel)
    media.addEventListener('seeking', onSeeking)
    media.addEventListener('seeked', onSeeked)
    schedule()
    return () => {
      disposed = true
      cancel()
      media.removeEventListener('play', start)
      media.removeEventListener('pause', cancel)
      media.removeEventListener('seeking', onSeeking)
      media.removeEventListener('seeked', onSeeked)
    }
  }, [handleVideoTimeUpdate, page?.playback?.video_stream_url])

  useEffect(() => {
    if (!initialPlaybackRequest) {
      return
    }

    if (!handoff && route.params.annotation && !selectedAnnotation?.id) {
      return
    }

    if (!handoff && route.params.clip && !activeClipId) {
      return
    }

    if (initialMomentPlaybackHandledRef.current) {
      return
    }

    initialMomentPlaybackHandledRef.current = true
    pendingPlaybackRef.current = initialPlaybackRequest
    focusVideoMoment(initialPlaybackRequest.seekMs, initialPlaybackRequest.autoplay)
  }, [activeClipId, handoff, initialPlaybackRequest, route.params.annotation, route.params.clip, selectedAnnotation?.id])

  const handleAnnotationSelection = useCallback((annotationId, { preserveCamera = false } = {}) => {
    const annotation = annotationMap.get(annotationId)
    if (!annotation) {
      return
    }

    const nextClip = (annotation.related_clip_ids || []).map((clipId) => clipMap.get(clipId)).find(Boolean) || null
    setTranscriptAutoFollow(true)
    setSelectedSearchResultId('')
    selectAnnotationWithCamera(annotation.id, preserveCamera)
    setActiveClipId(nextClip?.id || '')
    setMomentSource('annotation')
    // Play ONLY this moment's clip sequence (single clip stops at its end;
    // multi-clip auto-advances then stops) — Studio only.
    armMomentPlayback(buildMomentPlaybackClips(annotation, clipMap), 0)
    focusVideoMoment(nextClip?.start_ms ?? annotation.start_ms, true)
  }, [annotationMap, armMomentPlayback, clipMap, focusVideoMoment, selectAnnotationWithCamera])

  // Studio P3 — rail dispatch. A moment routes through the existing annotation
  // selection (sets selectedAnnotationId so the model focus follows); an auto
  // clip seeks the shared clock to the clip start. Either way the seek goes
  // through the same shared video clock as every other selector.
  const selectStudioRailItem = useCallback((item) => {
    if (!item) {
      return
    }
    if (item.kind === 'moment' && item.annotationId) {
      handleAnnotationSelection(item.annotationId)
      return
    }
    if (item.kind === 'clip' && item.clipId) {
      const clip = clipMap.get(item.clipId)
      setTranscriptAutoFollow(true)
      setSelectedSearchResultId('')
      selectAnnotationWithCamera('')
      setActiveClipId(item.clipId)
      setMomentSource('clip')
      // A standalone auto-clip plays only its own window, then stops.
      const startMs = Math.max(0, Math.floor(Number(clip?.start_ms ?? item.startMs ?? 0)))
      const endMs = Number.isFinite(Number(clip?.end_ms ?? item.endMs))
        ? Math.floor(Number(clip?.end_ms ?? item.endMs))
        : null
      armMomentPlayback(endMs !== null && endMs > startMs ? [{ id: item.clipId, startMs, endMs }] : [], 0)
      focusVideoMoment(startMs, true)
    }
  }, [armMomentPlayback, clipMap, focusVideoMoment, handleAnnotationSelection, selectAnnotationWithCamera])

  const handleCanvasAnnotationSelect = useCallback((annotation) => {
    if (!annotation?.id) {
      return
    }

    handleAnnotationSelection(annotation.id, { preserveCamera: true })
  }, [handleAnnotationSelection])

  // Studio P4 — search "Jump": an on-page result seeks the current recording in
  // place, preserving the object/session (same shared clock path as the existing
  // in-recording search selection).
  const handleStudioSearchJump = useCallback((result) => {
    if (!result) {
      return
    }
    const seekMs = Number.isFinite(Number(result.start_ms))
      ? Math.max(0, Math.floor(Number(result.start_ms)))
      : Math.max(0, Math.floor(Number(result.context_start_ms) || 0))
    disarmMomentPlayback()
    setTranscriptAutoFollow(true)
    selectAnnotationWithCamera('')
    setActiveClipId('')
    setMomentSource('video')
    setSelectedSearchResultId(result.segment_id || '')
    focusVideoMoment(seekMs, false)
    // Keep the focused media/stage visible after a Jump (the search panel sits
    // above the stage; without this the seeked moment is off-screen, especially
    // on mobile). Smooth unless reduced-motion is preferred.
    if (typeof window !== 'undefined') {
      window.requestAnimationFrame(() => {
        const stage = document.querySelector('.studio-shell .studio-stage')
        if (stage && typeof stage.scrollIntoView === 'function') {
          const prefersReduced = typeof window.matchMedia === 'function'
            && window.matchMedia('(prefers-reduced-motion: reduce)').matches
          stage.scrollIntoView({ behavior: prefersReduced ? 'auto' : 'smooth', block: 'start' })
        }
      })
    }
  }, [disarmMomentPlayback, focusVideoMoment, selectAnnotationWithCamera])

  // Studio P4 — search "Open": a cross-collection result navigates to its public
  // evidence deep link. Same-origin /evidence/objects/ guard (defense in depth;
  // StudioSearch already validates).
  const handleStudioSearchOpen = useCallback((result) => {
    const url = result?.evidence_url
    if (!isSafeEvidenceUrl(url) || typeof window === 'undefined') {
      return
    }
    window.location.assign(url)
  }, [])

  function handleSequenceSelection(clipId) {
    const clip = clipMap.get(clipId)
    if (!clip) {
      return
    }

    setTranscriptAutoFollow(true)
    setSelectedSearchResultId('')
    setActiveClipId(clip.id)
    setMomentSource('clip')
    // Continue the selected moment's sequence from the chosen clip, then stop
    // after the final clip.
    const sequence = buildMomentPlaybackClips(selectedAnnotation, clipMap)
    const startIndex = sequence.findIndex((entry) => entry.id === clip.id)
    if (startIndex >= 0) {
      armMomentPlayback(sequence, startIndex)
    } else {
      const startMs = Math.max(0, Math.floor(Number(clip.start_ms || 0)))
      const endMs = Number.isFinite(Number(clip.end_ms)) ? Math.floor(Number(clip.end_ms)) : null
      armMomentPlayback(endMs !== null && endMs > startMs ? [{ id: clip.id, startMs, endMs }] : [], 0)
    }
    focusVideoMoment(clip.start_ms, true)
  }

  function focusTranscriptRow(index) {
    const row = transcriptRowRefs.current.get(index)
    if (!(row instanceof HTMLElement)) {
      return
    }

    row.focus()
    if (transcriptListRef.current) {
      transcriptProgrammaticScrollRef.current = true
      const nextScrollTop = scrollTranscriptRowIntoView(transcriptListRef.current, row)
      scheduleTranscriptScrollUrlSync(nextScrollTop)
    }
  }

  function handleTranscriptRowKeyDown(index, event) {
    const transcriptCount = page?.transcript?.segments?.length || 0
    if (!transcriptCount) {
      return
    }

    let nextIndex = null
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowRight':
        nextIndex = Math.min(transcriptCount - 1, index + 1)
        break
      case 'ArrowUp':
      case 'ArrowLeft':
        nextIndex = Math.max(0, index - 1)
        break
      case 'Home':
        nextIndex = 0
        break
      case 'End':
        nextIndex = transcriptCount - 1
        break
      default:
        return
    }

    event.preventDefault()
    focusTranscriptRow(nextIndex)
  }

  function clearEvidenceSearch() {
    evidenceSearchRequestIdRef.current += 1
    setEvidenceSearchQuery('')
    setEvidenceSearchSubmittedQuery('')
    setEvidenceSearchResults([])
    setEvidenceSearchLoading(false)
    setEvidenceSearchError('')
    setEvidenceSearchHasRun(false)
    setEvidenceSearchExpanded(false)
    setSelectedSearchResultId('')
    setMomentSource(handoff?.source || page?.focus?.source || 'default')
    setEvidenceSearchMeta({
      totalResults: 0,
      resultWindow: 15,
      resultWindowCapped: false,
    })
  }

  function handleEvidenceSearchInputChange(nextQuery) {
    const normalizedQuery = String(nextQuery ?? '')

    if (!normalizedQuery.trim() && hasActiveEvidenceSearchState) {
      clearEvidenceSearch()
      return
    }

    setEvidenceSearchQuery(normalizedQuery)
  }

  function openPublicBrowse() {
    navigateToPublicBrowse()
  }

  function focusEvidenceSearchInput() {
    const target = searchInputRef.current
    if (!target) {
      return
    }

    target.focus()
    target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' })
  }

  function openCollectionRecord() {
    const targetUrl = normalizeQueryValue(page?.object?.external_url || '')
    if (!targetUrl) {
      openPublicBrowse()
      return
    }

    window.open(targetUrl, '_blank', 'noopener,noreferrer')
  }

  function handleEvidenceSearchResultSelection(result) {
    if (!result) {
      return
    }

    setTranscriptAutoFollow(true)
    selectAnnotationWithCamera('')
    setActiveClipId('')
    setMomentSource('video')
    setSelectedSearchResultId(result.segment_id)
    setEvidenceSearchExpanded(false)
    focusVideoMoment(result.start_ms, false)
  }

  async function runEvidenceSearch(queryOverride = null) {
    const query = String(queryOverride ?? evidenceSearchQuery ?? '').trim()

    if (!query) {
      if (hasActiveEvidenceSearchState) {
        clearEvidenceSearch()
      }

      return
    }

    if (!searchVideoId) {
      setEvidenceSearchError('This evidence page does not have a published recording to search.')
      setEvidenceSearchResults([])
      setEvidenceSearchHasRun(false)
      return
    }

    const requestId = evidenceSearchRequestIdRef.current + 1
    evidenceSearchRequestIdRef.current = requestId

    setEvidenceSearchLoading(true)
    setEvidenceSearchError('')
    setEvidenceSearchHasRun(true)
    setEvidenceSearchSubmittedQuery(query)
    setEvidenceSearchExpanded(true)
    setSelectedSearchResultId('')
    setEvidenceSearchResults([])
    setEvidenceSearchMeta({
      totalResults: 0,
      resultWindow: 15,
      resultWindowCapped: false,
    })

    try {
      const payload = await searchPublicSegments({
        query,
        video_id: searchVideoId,
        retrieval_mode: 'combined',
        page: 1,
        page_size: 5,
        limit: 15,
      })

      if (evidenceSearchRequestIdRef.current !== requestId) {
        return
      }

      setEvidenceSearchResults(Array.isArray(payload?.results) ? payload.results : [])
      setEvidenceSearchMeta({
        totalResults: Number(payload?.total_results || 0),
        resultWindow: Number(payload?.result_window || 15),
        resultWindowCapped: Boolean(payload?.result_window_capped),
      })
    } catch (error) {
      if (evidenceSearchRequestIdRef.current !== requestId) {
        return
      }

      setEvidenceSearchResults([])
      setEvidenceSearchMeta({
        totalResults: 0,
        resultWindow: 15,
        resultWindowCapped: false,
      })
      setEvidenceSearchError(describeEvidenceSearchError(error))
    } finally {
      if (evidenceSearchRequestIdRef.current === requestId) {
        setEvidenceSearchLoading(false)
      }
    }
  }

  async function handleEvidenceSearchSubmit(event) {
    event.preventDefault()
    await runEvidenceSearch()
  }

  useLayoutEffect(() => {
    if (!Number.isFinite(requestedTranscriptScrollTop)) {
      return
    }

    const container = transcriptListRef.current
    if (!(container instanceof HTMLElement)) {
      return
    }

    let cancelled = false
    const timeoutIds = []

    const restoreScrollTop = (attempt = 0) => {
      if (cancelled) {
        return
      }

      if (!container.clientHeight) {
        if (attempt < 4) {
          const retryId = window.setTimeout(() => restoreScrollTop(attempt + 1), 120)
          timeoutIds.push(retryId)
        }
        return
      }

      if (Math.abs(container.scrollTop - requestedTranscriptScrollTop) > EVIDENCE_TRANSCRIPT_SCROLL_RESTORE_TOLERANCE_PX) {
        applyTranscriptScrollTop(container, requestedTranscriptScrollTop)
      }
    }

    const animationFrameId = window.requestAnimationFrame(() => restoreScrollTop(0))

    return () => {
      cancelled = true
      window.cancelAnimationFrame(animationFrameId)
      timeoutIds.forEach((timeoutId) => window.clearTimeout(timeoutId))
    }
  }, [applyTranscriptScrollTop, compactContextView, page?.transcript?.video_id, requestedTranscriptScrollTop])

  const transcriptFocusIndex = useMemo(
    () => findFocusSegmentIndex(page?.transcript?.segments || [], activeFocus),
    [activeFocus, page?.transcript?.segments]
  )

  const activeSegmentIndex = playbackSegmentIndex >= 0 ? playbackSegmentIndex : transcriptFocusIndex
  const transcriptScrollIndex = activeSegmentIndex >= 0 ? activeSegmentIndex : transcriptFocusIndex

  useLayoutEffect(() => {
    if (!transcriptAutoFollow) {
      return
    }

    if (transcriptScrollIndex < 0) {
      return
    }

    // P2: on a PLAIN object load, skip the first auto-follow settle so the page
    // stays at the top; deep links still jump to the moment. Follow-along
    // resumes for subsequent active-segment changes (playback tracking).
    if (!initialAutoScrollHandledRef.current) {
      initialAutoScrollHandledRef.current = true
      if (!autoScrollOnLoad) {
        return
      }
    }

    const container = transcriptListRef.current
    const row = transcriptRowRefs.current.get(transcriptScrollIndex)
    if (!container || !row) {
      return
    }

    let cancelled = false
    const timeoutIds = []

    const ensureVisible = (attempt = 0) => {
      if (cancelled) {
        return
      }

      if (!container.clientHeight || !row.isConnected) {
        if (attempt < 4) {
          const retryId = window.setTimeout(() => ensureVisible(attempt + 1), 120)
          timeoutIds.push(retryId)
        }
        return
      }

      if (!rowComfortablyVisible(container, row)) {
        transcriptProgrammaticScrollRef.current = true
        const nextScrollTop = scrollTranscriptRowIntoView(container, row)
        scheduleTranscriptScrollUrlSync(nextScrollTop)
      }

      if (attempt < 4) {
        const retryDelay = attempt === 0 ? 140 : 220
        const retryId = window.setTimeout(() => {
          window.requestAnimationFrame(() => ensureVisible(attempt + 1))
        }, retryDelay)
        timeoutIds.push(retryId)
      }
    }

    const animationFrameId = window.requestAnimationFrame(() => ensureVisible(0))

    return () => {
      cancelled = true
      window.cancelAnimationFrame(animationFrameId)
      timeoutIds.forEach((timeoutId) => window.clearTimeout(timeoutId))
    }
  }, [autoScrollOnLoad, page?.transcript?.video_id, scheduleTranscriptScrollUrlSync, transcriptAutoFollow, transcriptScrollIndex])

  if (loading) {
    return <EvidenceLoadingState />
  }

  if (errorState || !page) {
    const resolvedErrorState = errorState || buildEvidencePageErrorState(null)
    let title = 'Evidence page unavailable'
    let message = 'The published evidence page is temporarily unavailable.'
    let primaryActionLabel = 'Return to homepage'
    let onPrimaryAction = openPublicBrowse
    let secondaryActionLabel = ''
    let onSecondaryAction = null

    if (resolvedErrorState.kind === 'parse') {
      title = 'Evidence link needs attention'
      message = 'This published evidence link cannot be opened as provided.'
    } else if (resolvedErrorState.kind === 'not_found') {
      title = 'Evidence page not found'
      message = 'The published evidence page you requested could not be found.'
    } else if (resolvedErrorState.kind === 'unauthorized') {
      title = 'Evidence page is protected'
      message = 'Access to this evidence page requires authorization.'
      primaryActionLabel = 'Reload protected page'
      onPrimaryAction = reloadProtectedEvidencePage
      secondaryActionLabel = 'Return to homepage'
      onSecondaryAction = openPublicBrowse
    } else {
      title = 'Connection interrupted'
      message = 'Loci could not reach this evidence page right now.'
      primaryActionLabel = 'Try again'
      onPrimaryAction = loadEvidencePage
      secondaryActionLabel = 'Return to homepage'
      onSecondaryAction = openPublicBrowse
    }

    return (
      <>
        <a
          className="skip-link"
          href={`#${EVIDENCE_MAIN_CONTENT_ID}`}
          onClick={() => focusSkipTarget(EVIDENCE_MAIN_CONTENT_ID)}
        >
          Skip to content
        </a>
        <main
          id={EVIDENCE_MAIN_CONTENT_ID}
          tabIndex={-1}
          className={`evidence-shell skip-link-target${studioEnabled ? ' studio-surface studio-not-found' : ''}`}
          data-studio-theme={studioEnabled ? resolveStudioPaletteSync() : undefined}
        >
          <div className="evidence-shell-inner single-state">
            <ErrorState
              kind={resolvedErrorState.kind}
              detail={resolvedErrorState.detail}
              kicker={studioEnabled ? '' : 'Loci Evidence'}
              title={title}
              message={message}
              className="evidence-state-card error"
              showBranding={true}
              primaryActionLabel={primaryActionLabel}
              onPrimaryAction={onPrimaryAction}
              secondaryActionLabel={secondaryActionLabel}
              onSecondaryAction={onSecondaryAction}
            />
          </div>
        </main>
      </>
    )
  }

  const modelUrl = resolveApiUrl(page.model?.model_url || '')
  // Client policy selects fixed allowlisted tokens. Declared object delivery
  // tiers also carry an exact-representation flag, so an unavailable active tier
  // reaches the recoverable viewer state instead of loading the large canonical
  // model. Ordinary objects preserve the established canonical fallback.
  const modelCanvasUrl = modelUrl
    ? buildModelRequestUrl(modelUrl, {
        gated: modelLoadPolicy.gated,
        highRisk: modelLoadPolicy.highRisk,
        deliveryCapabilities: page.model?.delivery_capabilities,
      })
    : modelUrl
  const modelCanvasDpr = isMobileEvidenceLayout ? 1 : EVIDENCE_MODEL_CANVAS_DPR
  // Gated (mobile / low-memory / Data Saver / constrained / iOS) and not yet
  // explicitly requested -> show the Load 3D model affordance.
  const modelCanvasDeferredForMobile = Boolean(modelLoadPolicy.gated && !mobileModelLoadRequested)
  const modelCanvasHighRisk = Boolean(modelCanvasDeferredForMobile && modelLoadPolicy.highRisk)
  const videoUrl = resolveApiUrl(page.playback?.video_stream_url || '')
  const videoPosterUrl = studioRailItems.find((item) => item.posterUrl)?.posterUrl || evidenceMetaImage
  const videoPreload = evidenceVideoPreload({
    hasFocusedMoment: routeHasFocusedMoment,
    hasHandoff: Boolean(handoff),
  })
  const videoAccessibleName = evidenceVideoAccessibleName({
    objectTitle: page.object.title,
    preload: videoPreload,
  })
  const videoDisplayTitle = evidenceVideoDisplayTitle({
    objectTitle: page.object.title,
    publishedTitle: page.playback?.video_title,
  })
  const timelineDurationMs = evidenceTimelineDurationMs({
    mediaDurationMs: studioDurationMs,
    publishedDurationMs: page.playback?.duration_ms,
  })
  const primaryEmptyActionLabel = page.object?.external_url ? 'Open collection record' : 'Return to homepage'
  const primaryEmptyAction = page.object?.external_url ? openCollectionRecord : openPublicBrowse
  const selectedCamera = selectedAnnotation?.camera || page.model?.default_camera || null

  function handleModelLoadRequest() {
    setMobileModelLoadRequested(true)
  }

  // Hoisted so the SAME element instance feeds both the legacy cards and the
  // flag-gated Studio stage (only one branch mounts per render).
  const videoElement = videoUrl ? (
    <video
      className="evidence-video-element"
      key={playbackKey}
      ref={videoRef}
      controls
      playsInline
      preload={videoPreload}
      poster={videoPosterUrl || undefined}
      aria-label={videoAccessibleName}
      src={videoUrl}
      onLoadedMetadata={(event) => {
        const target = event.currentTarget
        const pendingPlayback = pendingPlaybackRef.current
        if (pendingPlayback) {
          const requestComplete = commitPlaybackRequest(target, pendingPlayback, { allowAutoplay: false })
          if (requestComplete) {
            programmaticSeekRef.current = false
            pendingPlaybackRef.current = null
            appliedSeekKeyRef.current = playbackKey
          }
          return
        }
        if (appliedSeekKeyRef.current === playbackKey || !initialPlaybackRequest) {
          return
        }
        pendingPlaybackRef.current = initialPlaybackRequest
        const requestComplete = commitPlaybackRequest(target, pendingPlaybackRef.current, { allowAutoplay: false })
        if (requestComplete) {
          programmaticSeekRef.current = false
          pendingPlaybackRef.current = null
          appliedSeekKeyRef.current = playbackKey
        }
      }}
      onLoadedData={() => {
        setVideoLoadError('')
      }}
      onCanPlay={(event) => {
        const pendingPlayback = pendingPlaybackRef.current
        if (!pendingPlayback) {
          return
        }

        const requestComplete = commitPlaybackRequest(event.currentTarget, pendingPlayback, { allowAutoplay: true })
        if (!requestComplete) {
          return
        }

        programmaticSeekRef.current = false
        pendingPlaybackRef.current = null
        appliedSeekKeyRef.current = playbackKey
      }}
      onSeeked={(event) => {
        const pendingPlayback = pendingPlaybackRef.current
        if (!pendingPlayback) {
          const nextMs = Math.max(0, Math.floor(event.currentTarget.currentTime * 1000))
          const programmaticSeekTargetMs = programmaticSeekTargetMsRef.current
          if (
            programmaticSeekRef.current
            || (Number.isFinite(programmaticSeekTargetMs) && Math.abs(nextMs - programmaticSeekTargetMs) <= 350)
          ) {
            programmaticSeekRef.current = false
            programmaticSeekTargetMsRef.current = null
            return
          }

          // A manual scrub (native controls) ends guided moment playback so the
          // video plays through from where the user landed.
          momentPlaybackRef.current = { active: false, clips: [], index: 0 }
          lastReportedPlaybackMsRef.current = nextMs
          setCurrentMs(nextMs)
          setTranscriptAutoFollow(true)
          setSelectedSearchResultId('')
          selectAnnotationWithCamera('')
          setActiveClipId('')
          setMomentSource('video')
          return
        }

        const requestComplete = commitPlaybackRequest(event.currentTarget, pendingPlayback, { allowAutoplay: true })
        if (!requestComplete) {
          return
        }

        programmaticSeekRef.current = false
        programmaticSeekTargetMsRef.current = null
        pendingPlaybackRef.current = null
        appliedSeekKeyRef.current = playbackKey
      }}
      onTimeUpdate={(event) => {
        handleVideoTimeUpdate(event)
      }}
      onDurationChange={(event) => {
        const mediaDuration = Number(event.currentTarget.duration)
        if (Number.isFinite(mediaDuration) && mediaDuration > 0) {
          setStudioDurationMs(Math.floor(mediaDuration * 1000))
        }
      }}
      onError={() => {
        setVideoLoadError('Published video playback could not be loaded right now.')
      }}
    />
  ) : null

  const videoCard = (
    <section
      className="evidence-card evidence-video-card motion-evidence-card motion-evidence-assemble"
      style={buildMotionDelayStyle(180)}
    >
      <SectionHeading
        eyebrow="Video context"
        title={videoDisplayTitle}
      />

      {visibleClipSequence.length > 0 ? (
        <LinkedClipSequence
          clips={visibleClipSequence}
          activeClipId={visibleSequenceActiveClipId}
          onSelect={handleSequenceSelection}
        />
      ) : null}

      {videoElement ? (
        <>
          <div className="evidence-video-stage">
            {videoElement}
          </div>
          {videoLoadError ? <p className="error">{videoLoadError}</p> : null}
        </>
      ) : (
        <div className="evidence-video-stage evidence-video-stage-empty">
          <ActionNote
            title="No published video"
            text="This evidence page does not include published video playback."
            actionLabel={primaryEmptyActionLabel}
            onAction={primaryEmptyAction}
            className="evidence-video-placeholder"
          />
        </div>
      )}
    </section>
  )

  const transcriptCardContent = (
    <>
      <SectionHeading
        eyebrow="Transcript context"
        title="Supporting lines"
        className="evidence-section-heading-secondary evidence-transcript-heading"
      />

      {page.transcript?.segments?.length ? (
        <div className="evidence-transcript-list" ref={transcriptListRef} onScroll={handleTranscriptListScroll}>
          {page.transcript.segments.map((segment, index) => {
            const isActive = index === activeSegmentIndex
            const inWindow = segmentInFocusWindow(segment, activeFocus)
            const className = [
              'evidence-transcript-row',
              isActive ? 'active' : '',
              inWindow ? 'in-window' : ''
            ].filter(Boolean).join(' ')

            return (
              <button
                key={`${segment.position}:${segment.start_ms}:${segment.end_ms}`}
                ref={(node) => {
                  if (node) {
                    transcriptRowRefs.current.set(index, node)
                    return
                  }
                  transcriptRowRefs.current.delete(index)
                }}
                type="button"
                className={className}
                data-active={isActive ? 'true' : 'false'}
                aria-current={isActive ? 'true' : undefined}
                onKeyDown={(event) => handleTranscriptRowKeyDown(index, event)}
                onClick={() => {
                  setTranscriptAutoFollow(true)
                  setSelectedSearchResultId('')
                  selectAnnotationWithCamera('')
                  setActiveClipId('')
                  setMomentSource('video')
                  setCurrentMs(segment.start_ms)
                  focusVideoMoment(segment.start_ms, false)
                }}
              >
                <time>{formatClock(segment.start_ms)} - {formatClock(segment.end_ms)}</time>
                <strong>{segment.text}</strong>
              </button>
            )
          })}
        </div>
      ) : (
        <ActionNote
          title="No transcript context"
          text="This video does not include published transcript context."
          actionLabel={primaryEmptyActionLabel}
          onAction={primaryEmptyAction}
        />
      )}
    </>
  )

  const transcriptCard = (
    <section
      className="evidence-card evidence-transcript-card motion-evidence-card motion-evidence-assemble evidence-transcript-full"
      style={buildMotionDelayStyle(240)}
    >
      {transcriptCardContent}
    </section>
  )

  // Hoisted so the SAME ModelCanvas instance feeds both the legacy card and the
  // flag-gated Studio stage (only one branch mounts per render). This preserves
  // GLB load + WebGL context across the legacy/Studio boundary and theme changes.
  const modelStageElement = modelUrl ? (
    shouldMountModelCanvas ? (
      <Suspense
        fallback={(
          <DeferredModelCanvasPlaceholder
            eyebrow="Published object"
            title="Preparing 3D context..."
            message="Loading the published model and the selected evidence position."
            backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
          />
        )}
      >
        <LazyModelCanvas
          modelUrl={modelCanvasUrl}
          modelTransform={null}
          defaultCameraView={selectedCamera}
          annotations={selectedAnnotationMarkers}
          selectedAnnotationId={selectedAnnotationId}
          placementMode={false}
          cameraViewKey={`evidence:${selectedAnnotationId || page.focus.video_id || page.object.id}:${modelCameraSelection.revision}`}
          canvasDpr={modelCanvasDpr}
          preferLowPower={true}
          showAnnotationLabels={selectedAnnotationMarkers.length > 0}
          focusAnnotation={studioFocusAnnotation}
          preserveCameraOnSelection={Boolean(selectedAnnotationId && modelCameraSelection.annotationId === selectedAnnotationId)}
          enableAmbientMotion={studioEnabled && !modelLoadPolicy.gated}
          fadePinsWhenAway={studioEnabled}
          showViewerHelp={studioEnabled}
          suspendAmbientMotion={studioEnabled && citationDialogOpen}
          backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
          loadingEyebrow="Published object"
          loadingTitle="Preparing 3D context..."
          loadingMessage="Loading the published model and the selected evidence position."
          emptyTitle="No published model"
          emptyMessage="This evidence page does not include a published 3D model."
          onSelectAnnotation={handleCanvasAnnotationSelect}
        />
      </Suspense>
    ) : (
      <DeferredModelCanvasPlaceholder
        eyebrow="Published object"
        title={modelCanvasDeferredForMobile ? '3D model ready' : 'Preparing 3D context...'}
        message={modelCanvasDeferredForMobile
          ? (modelCanvasHighRisk
              ? 'This is a large, detailed model. On some iPhones it can be slow or unstable — loading it is optional.'
              : 'Load the interactive object view when you need the 3D context.')
          : 'Loading the published model and the selected evidence position.'}
        actionLabel={modelCanvasDeferredForMobile ? 'Load 3D model' : ''}
        onAction={modelCanvasDeferredForMobile ? handleModelLoadRequest : undefined}
        busy={!modelCanvasDeferredForMobile}
        highRisk={modelCanvasHighRisk}
        backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
      />
    )
  ) : (
    <ActionNote
      title="No published model"
      text="This evidence page does not include a published 3D model."
      actionLabel={primaryEmptyActionLabel}
      onAction={primaryEmptyAction}
      className="evidence-empty-note evidence-model-empty-note"
    />
  )

  const modelCardContent = (
    <>
      <SectionHeading
        eyebrow="3D context"
        title={selectedAnnotation ? selectedAnnotation.title : 'Published object view'}
      />

      {modelStageElement}

      {selectedAnnotation ? (
        <div className="evidence-model-note">
          <span className="public-experience-eyebrow">Selected detail</span>
          <strong>{selectedAnnotation.title}</strong>
          {selectedAnnotation.description ? <p className="muted">{selectedAnnotation.description}</p> : null}
        </div>
      ) : momentSource === 'clip' ? (
        <div className="evidence-model-note">
          <span className="public-experience-eyebrow">3D context note</span>
          <strong>No single published moment is attached to this clip.</strong>
          <p className="muted">The clip remains available within the full recorded context.</p>
        </div>
      ) : null}
    </>
  )

  const modelCard = (
    <section
      className="evidence-card evidence-model-card motion-evidence-card motion-evidence-pivot"
      style={buildMotionDelayStyle(120)}
    >
      {modelCardContent}
    </section>
  )

  const evidenceSearchCard = showEvidenceSearch ? (
    <section
      className="evidence-card evidence-search-card motion-evidence-card motion-evidence-assemble"
      style={buildMotionDelayStyle(220)}
    >
      <div className="evidence-search-header">
        <SectionHeading
          eyebrow="Search this recording"
          title="Find related transcript lines"
          description={`Search within ${videoDisplayTitle} and open matching moments in place.`}
        />

        {evidenceSearchHasResults ? (
          <div className="evidence-search-toolbar">
            <button
              type="button"
              className="search-result-button evidence-search-toolbar-button"
              onClick={() => setEvidenceSearchExpanded((current) => !current)}
              aria-expanded={evidenceSearchExpanded}
            >
              {evidenceSearchExpanded ? 'Hide matches' : `Show ${evidenceSearchResults.length} matches`}
            </button>
          </div>
        ) : null}
      </div>

      <form className="evidence-search-form" onSubmit={handleEvidenceSearchSubmit}>
        <SearchAutosuggest
          value={evidenceSearchQuery}
          onChange={handleEvidenceSearchInputChange}
          onSubmit={(query) => {
            setEvidenceSearchQuery(query)
            void runEvidenceSearch(query)
          }}
          placeholder="Search transcript text"
          inputRef={searchInputRef}
          onClear={clearEvidenceSearch}
          clearLabel="Clear recording search"
          inputProps={{
            type: 'search',
            'aria-label': 'Search this recording transcript',
          }}
        />
        <button type="submit" disabled={evidenceSearchLoading}>
          {evidenceSearchLoading ? 'Searching...' : 'Search'}
        </button>
      </form>

      {evidenceSearchHasRun ? (
        <div className="evidence-search-status">
          <div className="evidence-search-badges">
            {evidenceSearchSubmittedQuery ? (
              <span className="evidence-search-chip evidence-search-query-chip">
                Query: "{evidenceSearchSubmittedQuery}"
              </span>
            ) : null}
            {activeSearchResult ? (
              <span className="evidence-search-chip evidence-search-jump-chip">
                Last jump {formatClock(activeSearchResult.start_ms)} - {formatClock(activeSearchResult.end_ms)}
              </span>
            ) : null}
          </div>
          <div className="search-results-summary">
            <strong>{evidenceSearchSummary}</strong>
            {!evidenceSearchError ? <p>{evidenceSearchStatusNote}</p> : null}
          </div>
        </div>
      ) : (
        <p className="muted">{evidenceSearchStatusNote}</p>
      )}

      {evidenceSearchError ? <p className="error">{evidenceSearchError}</p> : null}

      {evidenceSearchHasResults && evidenceSearchExpanded ? (
        <div className="evidence-search-results">
          <ul className="list evidence-search-results-list">
            {evidenceSearchResults.map((result) => {
              const semanticTier = semanticFitTier(result.semantic_score)
              const isActiveResult = selectedSearchResultId === result.segment_id
              const contextWindow = surroundingContextRange(result)
              const resultThumbnailUrl = resolvePublicObjectPosterUrl(result.thumbnail_url || '')
              const hasSceneSupport = resultHasSceneSupport(result)
              const sceneSupportText = searchSceneSupportText(result)
              const resultDetails = searchResultContextDetails(result)
              const resultActionLabel = `Jump to ${formatClock(contextWindow.momentStart)}`

              return (
                <li
                  key={result.segment_id}
                  className={`search-result-row search-result-shell evidence-search-result-row ${isActiveResult ? 'active' : ''}`.trim()}
                >
                  <div className="search-result-media">
                    <ResultThumbnail
                      src={resultThumbnailUrl}
                      alt="Representative frame for this evidence search result"
                      fallbackTitle="Thumbnail unavailable"
                      fallbackNote="This search result does not yet have a usable published frame."
                    />
                  </div>

                  <div className="list-main video-list-main search-result-body">
                    <div className="search-result-heading">
                      <div className="search-result-heading-copy">
                        <p className="search-result-kicker">{videoDisplayTitle}</p>
                        <strong>Transcript moment {formatClock(contextWindow.momentStart)}-{formatClock(contextWindow.momentEnd)}</strong>
                        <span className="search-result-context">Transcript context {formatClock(contextWindow.contextStart)}-{formatClock(contextWindow.contextEnd)}</span>
                      </div>
                    </div>

                    <div className="search-result-evidence-group">
                      <div className="search-result-evidence-block">
                        <span className="search-result-evidence-label">Transcript line</span>
                        <p className="search-result-text">{result.text}</p>
                      </div>
                      {sceneSupportText ? (
                        <div className="search-result-evidence-block">
                          <span className="search-result-evidence-label">Scene support</span>
                          <p className="search-result-scene-line">{sceneSupportText}</p>
                        </div>
                      ) : null}
                    </div>

                    <p className="search-result-match-line">{searchMatchBasisLine(result)}</p>
                    {resultDetails.length ? (
                      <div className="search-result-detail-list" aria-label="Result context">
                        {resultDetails.map((detail) => (
                          <div key={detail.label} className="search-result-detail-item">
                            <span className="search-result-evidence-label">{detail.label}</span>
                            <p className="search-result-detail-value">{detail.value}</p>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>

                  <div className="search-result-rail" aria-label="Match support">
                    <div className="search-result-meta">
                      {result.lexical_match ? <span className="search-fit-chip lexical">Keyword match</span> : null}
                      {semanticTier ? (
                        <span className={`search-fit-chip semantic ${semanticTier.tone}`.trim()}>
                          {semanticTier.label}
                        </span>
                      ) : null}
                      {hasSceneSupport ? <span className="search-fit-chip visual">Scene support</span> : null}
                    </div>
                  </div>

                  <div className="search-result-actions">
                    <button
                      type="button"
                      className="search-result-button"
                      onClick={() => handleEvidenceSearchResultSelection(result)}
                    >
                      {resultActionLabel}
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        </div>
      ) : evidenceSearchHasRun && !evidenceSearchLoading && !evidenceSearchError && !evidenceSearchHasResults ? (
        <ActionNote
          title="No matching transcript lines"
          text={`No transcript lines matched "${evidenceSearchSubmittedQuery}".`}
          actionLabel="Search again"
          onAction={focusEvidenceSearchInput}
          className="evidence-empty-note evidence-search-empty-note"
        />
      ) : null}
    </section>
  ) : null

  const compactContextSurface = isCompactEvidenceLayout ? (
    <section
      className="evidence-card evidence-context-tabs-card motion-evidence-card motion-evidence-pivot"
      style={buildMotionDelayStyle(120)}
    >
      <div className="evidence-context-tab-bar" aria-label="Evidence context views">
        <button
          type="button"
          className={`evidence-context-tab ${compactContextView === 'model' ? 'active' : ''}`.trim()}
          aria-pressed={compactContextView === 'model'}
          onClick={() => setCompactContextView('model')}
        >
          3D context
        </button>
        <button
          type="button"
          className={`evidence-context-tab ${compactContextView === 'transcript' ? 'active' : ''}`.trim()}
          aria-pressed={compactContextView === 'transcript'}
          onClick={() => setCompactContextView('transcript')}
        >
          Transcript context
        </button>
      </div>

      {compactContextView === 'model' ? (
        <div className="evidence-context-tab-content evidence-model-card">
          {modelCardContent}
        </div>
      ) : (
        <div className="evidence-context-tab-content evidence-transcript-card evidence-transcript-full">
          {transcriptCardContent}
        </div>
      )}
    </section>
  ) : null

  const momentCard = (
    <article className="evidence-card evidence-moment-card evidence-moment-card-compact">
      <SectionHeading
        eyebrow="Selected evidence moment"
        title={currentMomentTitle}
        description={currentMomentSummary || undefined}
      />

      <div className="evidence-moment-meta">
        <span>{focusLabel(activeFocus.source)}</span>
        <span>{formatFocusWindow(activeFocus)}</span>
      </div>

      {excerpt ? <blockquote className="evidence-excerpt">{excerpt}</blockquote> : null}

      <section className="evidence-citation-block evidence-citation-block-compact" aria-label="Share this moment">
        <button
          type="button"
          className="evidence-citation-button"
          disabled={!citationCanonicalUrl}
          onClick={() => {
            void handleCitationCopy('link')
          }}
        >
          Copy link
        </button>
        <button
          type="button"
          className="evidence-citation-button ghost"
          disabled={!citationPlainText}
          onClick={openCitationDialog}
        >
          Copy citation
        </button>
      </section>
    </article>
  )

  const citationDialog = citationDialogOpen ? (
    <div
      className="evidence-citation-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          setCitationDialogOpen(false)
        }
      }}
    >
      <section
        className="evidence-citation-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evidence-citation-dialog-title"
        ref={citationDialogRef}
        onKeyDown={handleCitationDialogKeyDown}
      >
        <div className="evidence-citation-dialog-header">
          <div>
            <span className="public-experience-eyebrow">Citation</span>
            <h2 id="evidence-citation-dialog-title">Copy citation</h2>
          </div>
          <button
            type="button"
            className="evidence-citation-dialog-close"
            aria-label="Close citation dialog"
            ref={citationDialogInitialFocusRef}
            onClick={() => setCitationDialogOpen(false)}
          >
            Close
          </button>
        </div>

        <div className="evidence-citation-format-group" role="radiogroup" aria-label="Citation format">
          {CITATION_FORMAT_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`evidence-citation-format-button ${citationFormat === option.id ? 'active' : ''}`.trim()}
              role="radio"
              aria-checked={citationFormat === option.id}
              onClick={() => setCitationFormat(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <textarea
          className="evidence-citation-preview"
          readOnly
          value={formattedCitationText || citationPlainText}
          aria-label={`${selectedCitationFormatLabel} citation text`}
        />

        <details className="evidence-citation-dialog-details">
          <summary>Citation details</summary>
          <dl className="evidence-citation-meta">
            {citationMetaRows.map((row) => (
              <div key={row.label} className="evidence-citation-row">
                <dt className="evidence-citation-label">{row.label}</dt>
                <dd className="evidence-citation-value">{row.value}</dd>
              </div>
            ))}
            <div className="evidence-citation-row">
              <dt className="evidence-citation-label">Link</dt>
              <dd className="evidence-citation-value">
                {citationCanonicalUrl ? (
                  <a className="evidence-citation-link mono" href={citationCanonicalUrl} target="_blank" rel="noreferrer">
                    {citationCanonicalUrl}
                  </a>
                ) : 'Unavailable'}
              </dd>
            </div>
          </dl>
        </details>

        <div className="evidence-citation-dialog-actions">
          <button
            type="button"
            className="evidence-citation-button"
            disabled={!formattedCitationText && !citationPlainText}
            onClick={() => {
              void handleCitationCopy('plain')
            }}
          >
            Copy citation
          </button>
          <button
            type="button"
            className="evidence-citation-button ghost"
            onClick={() => setCitationDialogOpen(false)}
          >
            Done
          </button>
        </div>
      </section>
    </div>
  ) : null

  if (studioEnabled) {
    return (
      <>
        <a
          className="skip-link"
          href={`#${EVIDENCE_MAIN_CONTENT_ID}`}
          onClick={() => focusSkipTarget(EVIDENCE_MAIN_CONTENT_ID)}
        >
          Skip to content
        </a>
        <StudioShell
          mainContentId={EVIDENCE_MAIN_CONTENT_ID}
          autoScrollOnLoad={autoScrollOnLoad}
          objectTitle={page.object.title}
          objectSummary={page.object.summary || ''}
          objectExternalUrl={page.object.external_url || ''}
          projectName={page.object.project_name || page.object.project_slug || ''}
          onReturnHome={openPublicBrowse}
          activeMoment={studioActiveMoment}
          citeSlot={(
            <section className="studio-cite" aria-label="Cite or share this moment">
              <span className="studio-cite-label">
                <svg className="studio-cite-icon" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false">
                  <path d="M7 7h4v4H8.5c0 1.7.6 2.6 2 3l-.6 1.6C7.5 15.9 6.5 14.2 6.5 11.6V7H7zm7 0h4v4h-2.5c0 1.7.6 2.6 2 3l-.6 1.6c-2.4-.7-3.4-2.4-3.4-5V7z" fill="currentColor" />
                </svg>
                Cite this moment
              </span>
              <div className="studio-cite-actions">
                <button
                  type="button"
                  className="studio-cite-button"
                  disabled={!citationCanonicalUrl}
                  onClick={() => { void handleCitationCopy('link') }}
                >
                  Copy link
                </button>
                <button
                  type="button"
                  className="studio-cite-button ghost"
                  disabled={!citationPlainText}
                  onClick={openCitationDialog}
                >
                  Copy citation
                </button>
              </div>
            </section>
          )}
          videoTitle={videoDisplayTitle}
          modelSlot={modelStageElement}
          videoSlot={videoElement}
          currentMs={currentMs}
          durationMs={timelineDurationMs}
          onSeek={seekStudioMoment}
          formatClock={formatClock}
          transcriptSegments={page.transcript?.segments || []}
          activeSegmentIndex={activeSegmentIndex}
          railItems={studioRailItems}
          momentMarkers={studioMomentMarkers}
          selectedRailItemId={
            selectedAnnotationId
              ? `moment:${selectedAnnotationId}`
              : (activeClipId ? `clip:${activeClipId}` : '')
          }
          onSelectRailItem={selectStudioRailItem}
          clipSequence={studioClipSequence}
          activeClipId={activeClipId}
          onSelectClip={handleSequenceSelection}
          searchSlot={(
            <StudioSearch
              currentVideoStableId={page.playback?.video_id || ''}
              currentObjectPublicId={page.object?.id || ''}
              currentProjectSlug={page.object?.project_slug || ''}
              onJump={handleStudioSearchJump}
              onOpen={handleStudioSearchOpen}
              formatClock={formatClock}
            />
          )}
          overlaySlot={(
            <>
              {citationDialog}
              {citationFeedback ? (
                <p
                  className={`evidence-citation-status studio-citation-status ${citationFeedback.startsWith('Copy failed') ? 'error' : ''}`.trim()}
                  role="status"
                  aria-live="polite"
                >
                  {citationFeedback}
                </p>
              ) : null}
            </>
          )}
        />
      </>
    )
  }

  return (
    <>
      <a
        className="skip-link"
        href={`#${EVIDENCE_MAIN_CONTENT_ID}`}
        onClick={() => focusSkipTarget(EVIDENCE_MAIN_CONTENT_ID)}
      >
        Skip to content
      </a>
      <main
        id={EVIDENCE_MAIN_CONTENT_ID}
        tabIndex={-1}
        className="evidence-shell skip-link-target"
      >
        <div className="evidence-shell-inner">
        <header className="evidence-hero">
          <div className="evidence-hero-grid">
            <div className="evidence-hero-copy">
              <a
                className="surface-branding surface-branding-link evidence-branding"
                href={PUBLIC_BROWSE_PATH}
                aria-label="Go to the public homepage"
                onClick={(event) => {
                  event.preventDefault()
                  openPublicBrowse()
                }}
              >
                <BrandLockup variant="horizontal" />
                <p className="brand-tagline">Evidence, located.</p>
              </a>
              <h1>{page.object.title}</h1>
              {page.object.summary ? <p className="evidence-dek">{page.object.summary}</p> : null}
              <div className="evidence-hero-actions">
                <a
                  className="button-link secondary evidence-home-link"
                  href={PUBLIC_BROWSE_PATH}
                  onClick={(event) => {
                    event.preventDefault()
                    openPublicBrowse()
                  }}
                >
                  Return to homepage
                </a>
              </div>
            </div>
            <div className="evidence-hero-meta">
              <div>
                <span className="public-experience-eyebrow">Selected moment</span>
                <strong>{focusLabel(activeFocus.source)}</strong>
                <p className="muted">{formatFocusWindow(activeFocus)}</p>
              </div>
              <div>
                <span className="public-experience-eyebrow">Published video</span>
                <strong>{videoDisplayTitle}</strong>
                {page.object.project_name ? <p className="muted">{page.object.project_name}</p> : null}
              </div>
              {page.object.external_url ? (
                <a className="button-link secondary evidence-collection-link" href={page.object.external_url} target="_blank" rel="noreferrer">
                  Open collection record
                </a>
              ) : null}
            </div>
          </div>
        </header>

        <div className="evidence-top">
          {isCompactEvidenceLayout ? (
            <>
              {momentCard}
              {evidenceSearchCard}
              {videoCard}
            </>
          ) : (
            <>
              <section className="evidence-primary-column">
                {momentCard}
                {evidenceSearchCard}
                {videoCard}
              </section>
              <aside className="evidence-secondary-column">
                {modelCard}
                {transcriptCard}
              </aside>
            </>
          )}

          {compactContextSurface}
        </div>
        </div>
      </main>
      {citationDialog}
      {citationFeedback ? (
        <p className={`evidence-citation-status ${citationFeedback.startsWith('Copy failed') ? 'error' : ''}`.trim()} role="status" aria-live="polite">
          {citationFeedback}
        </p>
      ) : null}
    </>
  )
}
