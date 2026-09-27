import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import BrandLockup from '../components/BrandLockup'
import BrandMotion from '../components/BrandMotion'
import ErrorState, { buildErrorState } from '../components/ErrorState'
import { getEmbedObjectManifest, getPublicObjectPreview, resolveApiUrl, resolvePublicObjectPosterUrl } from '../lib/api'
import { useModelLoadPolicy } from '../lib/modelLoadPolicy'
import { buildModelRequestUrl } from '../lib/modelVariantUrl'
import { PUBLIC_BROWSE_PATH, navigateToPublicBrowse } from '../lib/navigation'
import { setPageMeta } from '../lib/seo'
import { isStudioSurfaceEnabled, useStudioSurfaceTheme } from '../public/studioSurface'
import useSemanticEmbedBridge from './useSemanticEmbedBridge'

const LazyModelCanvas = lazy(() => import('../components/ModelCanvas'))

function parseBooleanParam(value, fallback = false) {
  if (typeof value !== 'string') {
    return fallback
  }

  const normalized = value.trim().toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(normalized)) {
    return true
  }
  if (['0', 'false', 'no', 'off'].includes(normalized)) {
    return false
  }
  return fallback
}

function extractWebsiteObjectId(pathname) {
  const segments = pathname.split('/').filter(Boolean)
  if (segments[0] !== 'embed' || segments[1] !== 'object') {
    return ''
  }
  return decodeURIComponent(segments[2] || '')
}

function replaceEmbedCanonicalUrl(embedUrl) {
  if (typeof window === 'undefined' || typeof embedUrl !== 'string') {
    return
  }

  const trimmed = embedUrl.trim()
  if (!trimmed) {
    return
  }

  const currentUrl = new URL(window.location.href)
  const nextUrl = new URL(trimmed, currentUrl.origin)
  nextUrl.search = currentUrl.search
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

function extractPublicObjectId(modelSrc) {
  if (typeof modelSrc !== 'string') {
    return ''
  }

  const match = modelSrc.match(/\/api\/v1\/public\/objects\/([^/]+)\/model\/file/i)
  return match ? decodeURIComponent(match[1] || '') : ''
}

function buildCanvasAnnotations(manifest) {
  return (manifest?.annotations || []).map((annotation) => ({
    id: annotation.id,
    title: annotation.label || annotation.title || annotation.id,
    description: annotation.body || null,
    point_x: annotation.position[0],
    point_y: annotation.position[1],
    point_z: annotation.position[2],
    normal_x: annotation.normal?.[0] ?? null,
    normal_y: annotation.normal?.[1] ?? null,
    normal_z: annotation.normal?.[2] ?? null,
    camera_json: annotation.camera || null,
    review_status: 'ACTIVE'
  }))
}

function relationValues(annotation) {
  if (!annotation) {
    return []
  }

  return [
    ...(annotation.relatedPublicationIds || []),
    ...(annotation.relatedProjectIds || []),
    ...(annotation.relatedLocationIds || [])
  ]
}

function annotationRelatedClipIds(annotation) {
  if (!annotation) {
    return []
  }

  const normalized = []
  const append = (value) => {
    if (typeof value !== 'string') {
      return
    }

    const trimmed = value.trim()
    if (trimmed && !normalized.includes(trimmed)) {
      normalized.push(trimmed)
    }
  }

  if (Array.isArray(annotation.relatedClipIds)) {
    annotation.relatedClipIds.forEach(append)
  }
  append(annotation.relatedClipId)
  return normalized
}

function firstRelatedClipId(annotation) {
  return annotationRelatedClipIds(annotation)[0] || ''
}

function formatClipWindow(clip) {
  if (!clip) {
    return ''
  }

  const startTime = Number.isFinite(clip.startTime) ? `${clip.startTime}s` : '0s'
  return clip.endTime != null ? `${startTime} - ${clip.endTime}s` : startTime
}

function clampValue(value, min, max) {
  return Math.min(Math.max(value, min), max)
}

function normalizePreviewPlaylist(annotation) {
  if (!annotation) {
    return []
  }

  const source = Array.isArray(annotation.playlist) && annotation.playlist.length
    ? annotation.playlist
    : [{
        video_id: annotation.video_id,
        clip_id: annotation.clip_id || null,
        transcript_segment_id: annotation.transcript_segment_id || null,
        label: null,
        start_ms: annotation.start_ms,
        end_ms: annotation.end_ms
      }]

  return source.map((entry, index) => ({
    video_id: entry.video_id || '',
    clip_id: entry.clip_id || null,
    transcript_segment_id: entry.transcript_segment_id || null,
    label: entry.label || `Clip ${index + 1}`,
    start_ms: Number(entry.start_ms || 0),
    end_ms: Number(entry.end_ms || 0)
  }))
}

function normalizeTextValue(value) {
  if (typeof value !== 'string') {
    return ''
  }
  return value.trim().toLowerCase().replace(/\s+/g, ' ')
}

function normalizePositionVector(values) {
  if (!Array.isArray(values) || values.length !== 3) {
    return []
  }

  const normalized = values.map((value) => Number(value))
  return normalized.every((value) => Number.isFinite(value)) ? normalized : []
}

function annotationDistance(left, right) {
  if (left.length !== 3 || right.length !== 3) {
    return Number.POSITIVE_INFINITY
  }

  return Math.sqrt(
    ((left[0] - right[0]) ** 2)
    + ((left[1] - right[1]) ** 2)
    + ((left[2] - right[2]) ** 2)
  )
}

function previewAnnotationMatchScore(manifestAnnotation, previewAnnotation) {
  if (!manifestAnnotation || !previewAnnotation) {
    return 0
  }

  let score = 0
  const manifestTitle = normalizeTextValue(manifestAnnotation.label || manifestAnnotation.title || '')
  const previewTitle = normalizeTextValue(previewAnnotation.title || '')

  if (manifestTitle && previewTitle) {
    if (manifestTitle === previewTitle) {
      score += 8
    } else if (manifestTitle.includes(previewTitle) || previewTitle.includes(manifestTitle)) {
      score += 3
    }
  }

  const manifestBody = normalizeTextValue(manifestAnnotation.body || '')
  const previewBody = normalizeTextValue(previewAnnotation.description || '')
  if (manifestBody && previewBody && manifestBody === previewBody) {
    score += 3
  }

  const manifestPosition = normalizePositionVector(manifestAnnotation.position)
  const previewPosition = normalizePositionVector([
    previewAnnotation.point_x,
    previewAnnotation.point_y,
    previewAnnotation.point_z
  ])

  if (manifestPosition.length && previewPosition.length) {
    const distance = annotationDistance(manifestPosition, previewPosition)
    if (distance === 0) {
      score += 6
    } else if (distance < 0.0005) {
      score += 4
    } else if (distance < 0.005) {
      score += 2
    }
  }

  return score
}

function buildEmbedRouteErrorState(detail) {
  return buildErrorState(null, {
    kind: 'parse',
    parseDetail: detail,
  })
}

function buildEmbedManifestErrorState(error) {
  return buildErrorState(error, {
    fallbackKind: 'network',
    networkDetail: 'The published embed is temporarily unavailable.',
    notFoundDetail: 'This published embed could not be found.',
    unauthorizedDetail: 'This embed requires authorization.'
  })
}

function reloadProtectedEmbedPage() {
  window.location.assign(window.location.href)
}

function EmbedStageLoadingState({ compact = false, deferred = false, onAction, highRisk = false, backgroundColor = '#f5efe6' }) {
  const canLoad = deferred && typeof onAction === 'function'
  const loadCopy = highRisk
    ? 'This is a large, detailed model. On some iPhones it can be slow or unstable — loading it is optional.'
    : 'Load the interactive object view when you need the 3D context.'

  return (
    <div
      className={`embed-stage-loading ${compact ? 'compact' : ''} ${canLoad ? 'interactive' : ''} ${highRisk ? 'high-risk' : ''}`.trim()}
      role={canLoad ? undefined : 'status'}
      aria-live={canLoad ? undefined : 'polite'}
      style={{ background: backgroundColor }}
    >
      <div className="embed-stage-loading-card">
        <div className="embed-stage-loading-mark" aria-hidden="true">
          <BrandMotion name="pivot" size={compact ? 72 : 88} className="brand-motion-glow" />
        </div>
        <div className="embed-stage-loading-copy">
          <span className="embed-stage-loading-eyebrow">Published 3D view</span>
          <strong>{canLoad ? '3D model ready' : 'Preparing interactive 3D view...'}</strong>
          <p className={canLoad && highRisk ? 'embed-stage-loading-warning' : undefined}>
            {canLoad ? loadCopy : 'Loading model detail and linked moments for the first stable render.'}
          </p>
          {canLoad ? (
            <button type="button" className="embed-stage-load-action" onClick={onAction}>
              Load 3D model
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

export default function EmbedObjectApp() {
  const websiteObjectId = useMemo(() => extractWebsiteObjectId(window.location.pathname), [])
  const routeErrorState = useMemo(
    () => (!websiteObjectId ? buildEmbedRouteErrorState('This embed link does not point to a published object.') : null),
    [websiteObjectId]
  )
  const searchParams = useMemo(() => new URLSearchParams(window.location.search), [])
  const stageRef = useRef(null)
  const clipOverlayRef = useRef(null)
  const videoRef = useRef(null)
  const initialSelectionAppliedRef = useRef(false)
  const overlayPlaybackRequestRef = useRef(0)
  const manifestRequestIdRef = useRef(0)

  // P4.9 — Studio stage parity is the default public embed treatment after
  // approval; `?studio=0` / `?legacy=1` keeps the prior embed available.
  const studioEnabled = useMemo(() => isStudioSurfaceEnabled(window.location.search, { defaultEnabled: true }), [])
  const { palette: studioPalette } = useStudioSurfaceTheme()
  const studioSurfaceClass = studioEnabled ? ' studio-surface' : ''
  const studioThemeAttr = studioEnabled ? studioPalette : undefined

  const chromeMode = searchParams.get('chrome') === 'standard' ? 'standard' : 'minimal'
  const transcriptEnabled = parseBooleanParam(searchParams.get('transcript'), chromeMode === 'standard')
  const showRelated = parseBooleanParam(searchParams.get('related'), chromeMode === 'standard')
  const autoplayDefault = parseBooleanParam(searchParams.get('autoplay'), false)
  const initialAnnotationId = searchParams.get('annotation') || ''
  const initialClipId = searchParams.get('clip') || ''
  const embedTheme = searchParams.get('theme') || 'default'
  const embedSurface = searchParams.get('surface') || 'homepage'
  const titleEnabled = parseBooleanParam(searchParams.get('title'), true)
  const metadataEnabled = parseBooleanParam(searchParams.get('metadata'), true)
  const allowedOrigin = searchParams.get('origin') || ''
  const compactChrome = chromeMode === 'minimal'
  const homepageMinimalChrome = compactChrome && embedSurface === 'homepage'
  const autoOpenClipOnAnnotationSelection = homepageMinimalChrome || chromeMode === 'standard'

  const [manifest, setManifest] = useState(null)
  const [loading, setLoading] = useState(true)
  const [errorState, setErrorState] = useState(null)
  const [selectedAnnotationId, setSelectedAnnotationId] = useState('')
  const [activeClipId, setActiveClipId] = useState('')
  const [clipOpen, setClipOpen] = useState(false)
  const [autoplayClip, setAutoplayClip] = useState(false)
  const [clipOverlayPosition, setClipOverlayPosition] = useState(null)
  const [clipOverlayDragState, setClipOverlayDragState] = useState(null)
  const [cameraMode, setCameraMode] = useState('default')
  const [cameraResetKey, setCameraResetKey] = useState(0)
  const [modelReady, setModelReady] = useState(false)
  const [mobileModelLoadRequested, setMobileModelLoadRequested] = useState(false)
  const [publicPreview, setPublicPreview] = useState(null)
  const [previewVideoUrls, setPreviewVideoUrls] = useState({})
  const [activeSequenceIndex, setActiveSequenceIndex] = useState(0)
  const [sequencePlaybackActive, setSequencePlaybackActive] = useState(false)
  const [pendingOverlayPlayback, setPendingOverlayPlayback] = useState(null)
  const [overlayVideoSource, setOverlayVideoSource] = useState({ videoId: '', url: '' })
  // Non-blocking flag for a failed clip video load (media server unreachable).
  // The <video> element stays mounted; we only surface an inline caption.
  const [videoLoadError, setVideoLoadError] = useState(false)

  const annotations = useMemo(() => buildCanvasAnnotations(manifest), [manifest])
  const annotationMap = useMemo(
    () => new Map((manifest?.annotations || []).map((annotation) => [annotation.id, annotation])),
    [manifest]
  )
  const clipMap = useMemo(
    () => new Map((manifest?.clips || []).map((clip) => [clip.id, clip])),
    [manifest]
  )

  const selectedAnnotation = selectedAnnotationId ? annotationMap.get(selectedAnnotationId) || null : null
  const activeClip = clipOpen && activeClipId ? clipMap.get(activeClipId) || null : null
  const selectedAnnotationRelatedClipIds = useMemo(
    () => annotationRelatedClipIds(selectedAnnotation),
    [selectedAnnotation]
  )
  const selectedAnnotationClips = useMemo(
    () => selectedAnnotationRelatedClipIds.map((clipId) => clipMap.get(clipId)).filter(Boolean),
    [clipMap, selectedAnnotationRelatedClipIds]
  )
  const primaryRelatedClipId = selectedAnnotationRelatedClipIds[0] || ''
  const publicPreviewObjectId = useMemo(() => extractPublicObjectId(manifest?.modelSrc), [manifest?.modelSrc])
  const embedMetaImage = useMemo(() => {
    const posterSrc = typeof manifest?.posterSrc === 'string' ? manifest.posterSrc.trim() : ''
    return posterSrc ? resolvePublicObjectPosterUrl(posterSrc) : ''
  }, [manifest?.posterSrc])

  useEffect(() => {
    const manifestTitle = typeof manifest?.title === 'string' ? manifest.title.trim() : ''
    const objectName = manifestTitle || websiteObjectId
    const title = objectName || '3D Object Viewer'
    const description = objectName ? `Interactive 3D viewer and linked moments for ${objectName}.` : 'Interactive 3D viewer with linked moments.'

    setPageMeta({
      title,
      description,
      image: embedMetaImage,
      url: window.location.href,
      updateDocumentTitle: true,
    })
  }, [embedMetaImage, manifest?.title, websiteObjectId])

  // Adaptive 3D load decision (shared with evidence): desktop/laptop auto-loads;
  // mobile / low-memory / Data Saver / constrained-iframe / iOS keep the Load 3D
  // model gate. A full-width desktop iframe is NOT constrained, so it auto-loads.
  const modelLoadPolicy = useModelLoadPolicy(websiteObjectId)

  const previewAnnotationByManifestId = useMemo(() => {
    const mapping = new Map()
    const previewAnnotations = Array.isArray(publicPreview?.annotations) ? publicPreview.annotations : []
    const usedPreviewIds = new Set()

    for (const manifestAnnotation of manifest?.annotations || []) {
      let bestMatch = null
      let bestScore = 0

      for (const previewAnnotation of previewAnnotations) {
        if (!previewAnnotation?.id || usedPreviewIds.has(previewAnnotation.id)) {
          continue
        }

        const score = previewAnnotationMatchScore(manifestAnnotation, previewAnnotation)
        if (score > bestScore) {
          bestScore = score
          bestMatch = previewAnnotation
        }
      }

      if (bestMatch && bestScore >= 6) {
        mapping.set(manifestAnnotation.id, bestMatch)
        usedPreviewIds.add(bestMatch.id)
      }
    }

    return mapping
  }, [manifest?.annotations, publicPreview?.annotations])
  const selectedPreviewAnnotation = selectedAnnotation ? previewAnnotationByManifestId.get(selectedAnnotation.id) || null : null
  const selectedAnnotationPlaybackSequence = useMemo(() => {
    if (!selectedAnnotation) {
      return []
    }

    const previewPlaylist = normalizePreviewPlaylist(selectedPreviewAnnotation)
    if (previewPlaylist.length) {
      return previewPlaylist.map((entry, index) => {
        const websiteClipId = selectedAnnotationRelatedClipIds[index] || selectedAnnotationRelatedClipIds[0] || ''
        const fallbackClip = websiteClipId ? clipMap.get(websiteClipId) || null : null
        return {
          key: entry.clip_id || `${entry.video_id}:${entry.start_ms}:${entry.end_ms}:${index}`,
          label: `Clip ${index + 1}`,
          videoId: entry.video_id || '',
          startMs: Math.max(0, Math.floor(entry.start_ms || 0)),
          endMs: Math.max(0, Math.floor(entry.end_ms || entry.start_ms || 0)),
          websiteClipId,
          fallbackSrc: fallbackClip?.src || '',
          poster: fallbackClip?.poster || manifest?.posterSrc || ''
        }
      })
    }

    return selectedAnnotationClips.map((clip, index) => ({
      key: clip.id,
      label: `Clip ${index + 1}`,
      videoId: '',
      startMs: Math.max(0, Math.floor((clip.startTime || 0) * 1000)),
      endMs: Math.max(0, Math.floor(((clip.endTime ?? clip.startTime ?? 0)) * 1000)),
      websiteClipId: clip.id,
      fallbackSrc: clip.src,
      poster: clip.poster || manifest?.posterSrc || ''
    }))
  }, [clipMap, manifest?.posterSrc, selectedAnnotation, selectedAnnotationClips, selectedAnnotationRelatedClipIds, selectedPreviewAnnotation])
  const activePlaybackSegment = clipOpen ? selectedAnnotationPlaybackSequence[activeSequenceIndex] || null : null
  const compactOverlayOpen = clipOpen && Boolean(activeClip || activePlaybackSegment)
  const overlayUsesFullVideo = Boolean(activePlaybackSegment?.videoId && overlayVideoSource.url)
  const overlayVideoSrc = overlayUsesFullVideo ? overlayVideoSource.url : activeClip?.src || activePlaybackSegment?.fallbackSrc || ''
  const overlayVideoKey = overlayUsesFullVideo ? overlayVideoSource.videoId || 'embed-public-video' : activeClip?.id || activePlaybackSegment?.key || 'embed-clip-video'
  const appliedCameraView = cameraMode === 'annotation' && selectedAnnotation?.camera
    ? selectedAnnotation.camera
    : manifest?.initialView || null
  const embedModelSrc = manifest?.modelSrc || ''
  // Client policy selects fixed allowlisted tokens. Declared object delivery
  // tiers require their exact representation; ordinary objects preserve the
  // established canonical fallback. No user input becomes a storage path.
  const embedModelUrl = embedModelSrc
    ? buildModelRequestUrl(embedModelSrc, {
        gated: modelLoadPolicy.gated,
        highRisk: modelLoadPolicy.highRisk,
        deliveryCapabilities: manifest?.modelDelivery,
      })
    : embedModelSrc
  const modelDeferredForMobile = Boolean(
    modelLoadPolicy.gated
    && embedModelSrc
    && !mobileModelLoadRequested
    && !modelReady
  )
  const modelHighRisk = Boolean(modelDeferredForMobile && modelLoadPolicy.highRisk)
  const shouldRenderModelCanvas = Boolean(embedModelSrc && !modelDeferredForMobile)

  const handleMobileModelLoadRequest = useCallback(() => {
    setMobileModelLoadRequested(true)
  }, [])

  const getStateSnapshot = useCallback(() => ({
    objectId: websiteObjectId || null,
    manifestVersion: manifest?.manifestVersion || null,
    title: manifest?.title || null,
    ready: Boolean(manifest),
    hasInteractiveModel: Boolean(manifest?.modelSrc),
    modelReady: manifest?.modelSrc ? modelReady : Boolean(manifest),
    selectedAnnotationId: selectedAnnotation?.id || null,
    relatedClipId: primaryRelatedClipId || null,
    relatedClipIds: selectedAnnotationRelatedClipIds,
    activeClipId: activeClip?.id || null,
    chrome: chromeMode,
    relatedPublicationIds: selectedAnnotation?.relatedPublicationIds || [],
    relatedProjectIds: selectedAnnotation?.relatedProjectIds || [],
    relatedLocationIds: selectedAnnotation?.relatedLocationIds || []
  }), [
    activeClip?.id,
    chromeMode,
    manifest,
    modelReady,
    primaryRelatedClipId,
    selectedAnnotation,
    selectedAnnotationRelatedClipIds,
    websiteObjectId
  ])

  const closeClip = useCallback(() => {
    overlayPlaybackRequestRef.current += 1
    setClipOpen(false)
    setActiveClipId('')
    setAutoplayClip(false)
    setActiveSequenceIndex(0)
    setSequencePlaybackActive(false)
    setPendingOverlayPlayback(null)
    setOverlayVideoSource({ videoId: '', url: '' })
    setClipOverlayPosition(null)
    setClipOverlayDragState(null)
  }, [])

  const resetView = useCallback(() => {
    setCameraMode('default')
    setCameraResetKey((current) => current + 1)
    return true
  }, [])

  const ensurePreviewVideoUrl = useCallback(async (videoId) => {
    if (!videoId) {
      return ''
    }

    const cachedUrl = previewVideoUrls[videoId]
    if (cachedUrl) {
      return cachedUrl
    }

    if (!publicPreviewObjectId) {
      return ''
    }

    const payload = await getPublicObjectPreview(publicPreviewObjectId, { videoId })
    const resolvedUrl = resolveApiUrl(payload?.video_url)

    setPublicPreview((current) => current || payload)
    if (resolvedUrl) {
      setPreviewVideoUrls((current) => ({
        ...current,
        [videoId]: resolvedUrl
      }))
    }

    return resolvedUrl
  }, [previewVideoUrls, publicPreviewObjectId])

  const playAnnotationSegment = useCallback((annotation, clipIndex = 0, { autoplay = false } = {}) => {
    if (!annotation) {
      return false
    }

    const previewAnnotation = previewAnnotationByManifestId.get(annotation.id) || null
    const relatedClipIds = annotationRelatedClipIds(annotation)
    const previewPlaylist = normalizePreviewPlaylist(previewAnnotation)
    const playbackSequence = previewPlaylist.length
      ? previewPlaylist.map((entry, index) => {
          const websiteClipId = relatedClipIds[index] || relatedClipIds[0] || ''
          const fallbackClip = websiteClipId ? clipMap.get(websiteClipId) || null : null
          return {
            key: entry.clip_id || `${entry.video_id}:${entry.start_ms}:${entry.end_ms}:${index}`,
            label: `Clip ${index + 1}`,
            videoId: entry.video_id || '',
            startMs: Math.max(0, Math.floor(entry.start_ms || 0)),
            endMs: Math.max(0, Math.floor(entry.end_ms || entry.start_ms || 0)),
            websiteClipId,
            fallbackSrc: fallbackClip?.src || '',
            poster: fallbackClip?.poster || manifest?.posterSrc || ''
          }
        })
      : relatedClipIds.map((clipId, index) => {
          const clip = clipMap.get(clipId) || null
          return {
            key: clipId,
            label: `Clip ${index + 1}`,
            videoId: '',
            startMs: Math.max(0, Math.floor(((clip?.startTime || 0)) * 1000)),
            endMs: Math.max(0, Math.floor(((clip?.endTime ?? clip?.startTime ?? 0)) * 1000)),
            websiteClipId: clipId,
            fallbackSrc: clip?.src || '',
            poster: clip?.poster || manifest?.posterSrc || ''
          }
        })

    const segment = playbackSequence[clipIndex] || null
    if (!segment) {
      return false
    }

    const requestId = overlayPlaybackRequestRef.current + 1
    overlayPlaybackRequestRef.current = requestId

    setSelectedAnnotationId(annotation.id)
    setCameraMode('annotation')
    setClipOpen(true)
    setActiveSequenceIndex(clipIndex)
    setActiveClipId(segment.websiteClipId || '')
    setAutoplayClip(Boolean(autoplay))
    setSequencePlaybackActive(playbackSequence.length > 1 || Boolean(segment.videoId))
    setPendingOverlayPlayback(null)

    if (!segment.videoId) {
      setOverlayVideoSource({ videoId: '', url: '' })
      return Boolean(segment.fallbackSrc)
    }

    const queuePlayback = (url) => {
      if (!url || overlayPlaybackRequestRef.current !== requestId) {
        return
      }

      setOverlayVideoSource({ videoId: segment.videoId, url })
      setPendingOverlayPlayback({
        videoId: segment.videoId,
        seekMs: segment.startMs,
        autoplay: Boolean(autoplay)
      })
    }

    const cachedUrl = previewVideoUrls[segment.videoId]
    if (cachedUrl) {
      queuePlayback(cachedUrl)
      return true
    }

    ensurePreviewVideoUrl(segment.videoId)
      .then(queuePlayback)
      .catch(() => {
        if (overlayPlaybackRequestRef.current !== requestId) {
          return
        }
        setSequencePlaybackActive(false)
        setOverlayVideoSource({ videoId: '', url: '' })
      })

    return true
  }, [clipMap, ensurePreviewVideoUrl, manifest?.posterSrc, previewAnnotationByManifestId, previewVideoUrls])

  const applyPendingOverlayPlayback = useCallback((media) => {
    if (!media || !pendingOverlayPlayback) {
      return false
    }
    if (pendingOverlayPlayback.videoId && overlayVideoSource.videoId && pendingOverlayPlayback.videoId !== overlayVideoSource.videoId) {
      return false
    }
    if (media.readyState < 1) {
      return false
    }

    const target = Math.max(0, Math.floor(pendingOverlayPlayback.seekMs || 0))
    const durationMs = Number.isFinite(media.duration) ? Math.floor(media.duration * 1000) : null
    const clampedMs = durationMs && durationMs > 250 ? Math.min(target, durationMs - 250) : target
    media.currentTime = clampedMs / 1000

    const shouldAutoplay = Boolean(pendingOverlayPlayback.autoplay)
    setPendingOverlayPlayback(null)
    setAutoplayClip(false)

    if (shouldAutoplay) {
      media.play().catch(() => null)
    } else {
      media.pause()
    }

    return true
  }, [overlayVideoSource.videoId, pendingOverlayPlayback])

  const openClip = useCallback((clipId, { autoplay = false } = {}) => {
    const clip = clipMap.get(clipId)
    if (!clip) {
      return false
    }

    const matchingAnnotation = (manifest?.annotations || []).find((annotation) => annotationRelatedClipIds(annotation).includes(clipId)) || null
    if (matchingAnnotation) {
      setSelectedAnnotationId(matchingAnnotation.id)
      setCameraMode('annotation')

      if (autoOpenClipOnAnnotationSelection) {
        const sequence = annotationRelatedClipIds(matchingAnnotation)
        const clipIndex = Math.max(0, sequence.indexOf(clipId))
        if (playAnnotationSegment(matchingAnnotation, clipIndex, { autoplay })) {
          return true
        }
      }
    }

    setActiveSequenceIndex(0)
    setSequencePlaybackActive(false)
    setPendingOverlayPlayback(null)
    setOverlayVideoSource({ videoId: '', url: '' })
    setActiveClipId(clipId)
    setClipOpen(true)
    setAutoplayClip(Boolean(autoplay))
    return true
  }, [autoOpenClipOnAnnotationSelection, clipMap, manifest, playAnnotationSegment])

  const setAnnotation = useCallback((annotationId, { autoplay = false, source = 'local' } = {}) => {
    const annotation = annotationMap.get(annotationId)
    if (!annotation) {
      return false
    }

    setSelectedAnnotationId(annotation.id)
    setCameraMode('annotation')

    const playbackSequence = normalizePreviewPlaylist(previewAnnotationByManifestId.get(annotation.id) || null)
    const relatedClipId = firstRelatedClipId(annotation)
    if (autoOpenClipOnAnnotationSelection && source !== 'init' && (playbackSequence.length || relatedClipId)) {
      if (playAnnotationSegment(annotation, 0, { autoplay })) {
        return true
      }
    }

    if (!playbackSequence.length && !relatedClipId) {
      closeClip()
      return true
    }

    closeClip()
    return true
  }, [annotationMap, autoOpenClipOnAnnotationSelection, closeClip, playAnnotationSegment, previewAnnotationByManifestId])

  const firstInteractiveAnnotationId = useMemo(() => {
    const annotations = manifest?.annotations || []
    return annotations.find((annotation) => annotationRelatedClipIds(annotation).length > 0)?.id || annotations[0]?.id || ''
  }, [manifest?.annotations])

  const focusFirstMoment = useCallback(() => {
    if (firstInteractiveAnnotationId) {
      setAnnotation(firstInteractiveAnnotationId, { source: 'local' })
      return
    }

    resetView()
  }, [firstInteractiveAnnotationId, resetView, setAnnotation])

  useSemanticEmbedBridge({
    objectId: websiteObjectId,
    allowedOrigin,
    chromeMode,
    manifestLoaded: Boolean(manifest),
    hasInteractiveModel: Boolean(manifest?.modelSrc),
    modelReady,
    currentAnnotation: selectedAnnotation,
    currentClip: activeClip,
    errorMessage: errorState?.detail || routeErrorState?.detail || '',
    getStateSnapshot,
    onSetAnnotation: setAnnotation,
    onOpenClip: openClip,
    onResetView: resetView
  })

  const loadManifest = useCallback(async () => {
    const requestId = manifestRequestIdRef.current + 1
    manifestRequestIdRef.current = requestId
    initialSelectionAppliedRef.current = false
    setLoading(true)
    setErrorState(null)
    setManifest(null)
    setSelectedAnnotationId('')
    setActiveClipId('')
    setClipOpen(false)
    setAutoplayClip(false)
    setCameraMode('default')
    setCameraResetKey(0)
    setModelReady(false)
    setMobileModelLoadRequested(false)
    setPublicPreview(null)
    setPreviewVideoUrls({})
    setActiveSequenceIndex(0)
    setSequencePlaybackActive(false)
    setPendingOverlayPlayback(null)
    setOverlayVideoSource({ videoId: '', url: '' })
    setClipOverlayPosition(null)
    setClipOverlayDragState(null)

    if (!websiteObjectId) {
      setLoading(false)
      return
    }

    try {
      const payload = await getEmbedObjectManifest(websiteObjectId)
      if (manifestRequestIdRef.current !== requestId) {
        return
      }

      setManifest(payload)
      replaceEmbedCanonicalUrl(payload?.embedUrl)
      setModelReady(!payload.modelSrc)
    } catch (error) {
      if (manifestRequestIdRef.current !== requestId) {
        return
      }

      setErrorState(buildEmbedManifestErrorState(error))
    } finally {
      if (manifestRequestIdRef.current === requestId) {
        setLoading(false)
      }
    }
  }, [websiteObjectId])

  useEffect(() => {
    loadManifest()

    return () => {
      manifestRequestIdRef.current += 1
    }
  }, [loadManifest])

  useEffect(() => {
    let cancelled = false

    if (!publicPreviewObjectId) {
      return () => {
        cancelled = true
      }
    }

    getPublicObjectPreview(publicPreviewObjectId)
      .then((payload) => {
        if (cancelled) {
          return
        }

        setPublicPreview(payload)
        const videoId = payload?.video?.id || ''
        const videoUrl = resolveApiUrl(payload?.video_url)
        setPreviewVideoUrls(videoId && videoUrl ? { [videoId]: videoUrl } : {})
      })
      .catch(() => {
        if (cancelled) {
          return
        }
        setPublicPreview(null)
      })

    return () => {
      cancelled = true
    }
  }, [publicPreviewObjectId])

  useEffect(() => {
    if (!manifest || initialSelectionAppliedRef.current) {
      return
    }

    initialSelectionAppliedRef.current = true
    if (initialClipId && openClip(initialClipId, { autoplay: autoplayDefault, source: 'init' })) {
      return
    }
    if (initialAnnotationId && setAnnotation(initialAnnotationId, { autoplay: autoplayDefault, source: 'init' })) {
      return
    }
    if (chromeMode === 'standard' && manifest.annotations?.[0]) {
      setSelectedAnnotationId(manifest.annotations[0].id)
    }
  }, [autoplayDefault, chromeMode, initialAnnotationId, initialClipId, manifest, openClip, setAnnotation])

  useEffect(() => {
    if (overlayUsesFullVideo || !activeClip || !clipOpen || !autoplayClip || !videoRef.current) {
      return
    }

    const media = videoRef.current
    const startPlayback = async () => {
      try {
        media.currentTime = 0
        await media.play()
      } catch {
        // Ignore autoplay rejections from the browser and leave controls available.
      } finally {
        setAutoplayClip(false)
      }
    }

    if (media.readyState >= 1) {
      startPlayback()
    }
  }, [activeClip, autoplayClip, clipOpen, overlayUsesFullVideo])

  useEffect(() => {
    if (!videoRef.current) {
      return
    }

    applyPendingOverlayPlayback(videoRef.current)
  }, [applyPendingOverlayPlayback, overlayVideoSource.url, pendingOverlayPlayback])

  useEffect(() => {
    if (!clipOverlayDragState) {
      return undefined
    }

    const handlePointerMove = (event) => {
      if (event.pointerId !== clipOverlayDragState.pointerId) {
        return
      }
      if (!stageRef.current || !clipOverlayRef.current) {
        return
      }

      const stageRect = stageRef.current.getBoundingClientRect()
      const overlayRect = clipOverlayRef.current.getBoundingClientRect()
      const nextX = clampValue(
        event.clientX - stageRect.left - clipOverlayDragState.offsetX,
        0,
        Math.max(0, stageRect.width - overlayRect.width)
      )
      const nextY = clampValue(
        event.clientY - stageRect.top - clipOverlayDragState.offsetY,
        0,
        Math.max(0, stageRect.height - overlayRect.height)
      )

      setClipOverlayPosition({ x: nextX, y: nextY })
    }

    const stopDragging = (event) => {
      if (!event || event.pointerId === clipOverlayDragState.pointerId) {
        setClipOverlayDragState(null)
      }
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopDragging)
    window.addEventListener('pointercancel', stopDragging)

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopDragging)
      window.removeEventListener('pointercancel', stopDragging)
    }
  }, [clipOverlayDragState])

  useEffect(() => {
    if (!clipOverlayPosition || !stageRef.current || !clipOverlayRef.current) {
      return undefined
    }

    const clampOverlayToStage = () => {
      if (!stageRef.current || !clipOverlayRef.current) {
        return
      }
      const stageRect = stageRef.current.getBoundingClientRect()
      const overlayRect = clipOverlayRef.current.getBoundingClientRect()
      const currentPosition = clipOverlayPosition || {
        x: overlayRect.left - stageRect.left,
        y: overlayRect.top - stageRect.top
      }
      const boundedX = clampValue(currentPosition.x, 0, Math.max(0, stageRect.width - overlayRect.width))
      const boundedY = clampValue(currentPosition.y, 0, Math.max(0, stageRect.height - overlayRect.height))

      if (boundedX !== currentPosition.x || boundedY !== currentPosition.y) {
        setClipOverlayPosition({ x: boundedX, y: boundedY })
      }
    }

    window.addEventListener('resize', clampOverlayToStage)
    return () => window.removeEventListener('resize', clampOverlayToStage)
  }, [clipOverlayPosition])

  const handleSceneReadyChange = useCallback((ready) => {
    setModelReady(Boolean(ready))
  }, [])

  const handleClipOverlayDragStart = useCallback((event) => {
    if (event.button != null && event.button !== 0) {
      return
    }
    if (!stageRef.current || !clipOverlayRef.current) {
      return
    }
    if (event.target instanceof Element && event.target.closest('button')) {
      return
    }

    event.preventDefault()

    const stageRect = stageRef.current.getBoundingClientRect()
    const overlayRect = clipOverlayRef.current.getBoundingClientRect()
    const currentPosition = {
      x: overlayRect.left - stageRect.left,
      y: overlayRect.top - stageRect.top
    }

    setClipOverlayPosition(currentPosition)
    setClipOverlayDragState({
      pointerId: event.pointerId,
      offsetX: event.clientX - overlayRect.left,
      offsetY: event.clientY - overlayRect.top
    })
  }, [])

  const toggleFullscreen = useCallback(async () => {
    if (!stageRef.current || typeof document === 'undefined') {
      return
    }
    try {
      if (document.fullscreenElement === stageRef.current) {
        await document.exitFullscreen()
        return
      }
      await stageRef.current.requestFullscreen()
    } catch {
      // Ignore unsupported fullscreen requests.
    }
  }, [])

  const clipOverlayStyle = useMemo(() => {
    const style = {}

    if (clipOverlayPosition) {
      style.left = `${clipOverlayPosition.x}px`
      style.top = `${clipOverlayPosition.y}px`
      style.right = 'auto'
      style.bottom = 'auto'
    }

    return Object.keys(style).length ? style : undefined
  }, [clipOverlayPosition])

  const compactTitleVisible = Boolean(titleEnabled && manifest?.title)
  const compactSummaryVisible = Boolean(metadataEnabled && manifest?.summary)
  const compactHeaderVisible = compactTitleVisible || compactSummaryVisible
  // In Studio the toolbar row always renders (it carries the LOCI lockup), so the
  // stage card keeps its 3-row grid and the stage stays on the 1fr fill row.
  const compactHeaderlessStage = compactChrome && !compactHeaderVisible && !studioEnabled

  const compactStage = manifest ? (
    <section className={`embed-stage-card compact ${compactHeaderlessStage ? 'chromeless' : ''}`.trim()}>
      {(compactHeaderVisible || studioEnabled) ? (
        <div className="embed-stage-toolbar compact">
          {studioEnabled ? (
            <div className="embed-studio-brand" aria-label="Loci">
              <BrandLockup variant="horizontal" />
            </div>
          ) : null}
          {compactHeaderVisible ? (
            <div className="embed-stage-heading">
              {compactTitleVisible ? (
                <div className="embed-stage-heading-row">
                  <strong>{manifest.title}</strong>
                </div>
              ) : null}
              {compactSummaryVisible ? <span className="embed-stage-selection">{manifest.summary}</span> : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className={`embed-stage compact ${compactOverlayOpen ? 'has-clip' : ''} ${compactHeaderlessStage ? 'chromeless' : ''}`.trim()} ref={stageRef}>
        {shouldRenderModelCanvas ? (
          <div className={`embed-stage-canvas ${modelReady ? 'ready' : 'pending'}`.trim()}>
            <Suspense fallback={null}>
              <LazyModelCanvas
                modelUrl={embedModelUrl}
                modelTransform={null}
                defaultCameraView={appliedCameraView}
                annotations={annotations}
                selectedAnnotationId={selectedAnnotationId}
                placementMode={false}
                showAnnotationLabels={true}
                showAutoLoadingOverlay={false}
                backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
                loadingEyebrow="Loci embed"
                loadingTitle="Preparing interactive 3D view..."
                loadingMessage="Loading model detail and linked moments."
                emptyTitle="No 3D model"
                emptyMessage="This embed currently has no interactive 3D asset."
                cameraViewKey={`${cameraMode}:${cameraResetKey}:${selectedAnnotationId || 'none'}`}
                onSceneReadyChange={handleSceneReadyChange}
                onSelectAnnotation={(annotation) => setAnnotation(annotation.id, { autoplay: true, source: 'local' })}
              />
            </Suspense>
          </div>
        ) : null}

        {manifest.modelSrc && (!modelReady || modelDeferredForMobile) ? (
          <EmbedStageLoadingState
            compact={homepageMinimalChrome}
            deferred={modelDeferredForMobile}
            onAction={modelDeferredForMobile ? handleMobileModelLoadRequest : undefined}
            highRisk={modelHighRisk}
            backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
          />
        ) : null}

        {compactOverlayOpen ? (
          <aside
            ref={clipOverlayRef}
            className={`embed-compact-card embed-clip-overlay ${clipOverlayDragState ? 'dragging' : ''}`.trim()}
            style={clipOverlayStyle}
          >
            <div
              className="embed-compact-card-heading embed-clip-overlay-header"
              onPointerDown={handleClipOverlayDragStart}
            >
              <strong>{selectedAnnotation?.title || selectedAnnotation?.label || activeClip?.title || manifest.title}</strong>
              <button type="button" className="ghost embed-clip-overlay-close" onClick={closeClip} aria-label="Close clip overlay">
                X
              </button>
            </div>

            {selectedAnnotationPlaybackSequence.length > 1 ? (
              <div className="embed-mini-playlist" aria-label="Related clips">
                {selectedAnnotationPlaybackSequence.map((segment, index) => (
                  <button
                    type="button"
                    key={segment.key}
                    className={`embed-mini-clip-button ${index === activeSequenceIndex ? 'active' : ''}`.trim()}
                    onClick={() => selectedAnnotation && playAnnotationSegment(selectedAnnotation, index, { autoplay: true })}
                  >
                    {`Clip ${index + 1}`}
                  </button>
                ))}
              </div>
            ) : null}

            <div className="embed-clip-video-shell">
              {overlayVideoSrc ? (
                <video
                  key={overlayVideoKey}
                  ref={videoRef}
                  className="embed-clip-video compact"
                  src={overlayVideoSrc}
                  draggable={false}
                  controls
                  playsInline
                  preload="metadata"
                  onLoadedMetadata={() => {
                    setVideoLoadError(false)
                    if (applyPendingOverlayPlayback(videoRef.current)) {
                      return
                    }
                    if (!autoplayClip || !videoRef.current) {
                      return
                    }
                    videoRef.current.play().catch(() => null).finally(() => setAutoplayClip(false))
                  }}
                  onError={() => setVideoLoadError(true)}
                  onTimeUpdate={() => {
                    if (!sequencePlaybackActive || !selectedAnnotation || !activePlaybackSegment || !videoRef.current) {
                      return
                    }

                    const media = videoRef.current
                    const currentMs = Math.floor(media.currentTime * 1000)
                    if (currentMs + 80 < activePlaybackSegment.endMs) {
                      return
                    }

                    if (activeSequenceIndex + 1 < selectedAnnotationPlaybackSequence.length) {
                      playAnnotationSegment(selectedAnnotation, activeSequenceIndex + 1, { autoplay: true })
                      return
                    }

                    const stopAtMs = Math.max(activePlaybackSegment.startMs, activePlaybackSegment.endMs)
                    media.pause()
                    media.currentTime = stopAtMs / 1000
                    setSequencePlaybackActive(false)
                    setAutoplayClip(false)
                  }}
                />
              ) : (
                <div className="embed-clip-video-loading" role="status" aria-live="polite">
                  <strong>Loading source video...</strong>
                  <p>Preparing the published video stream for this annotation.</p>
                </div>
              )}
              {overlayVideoSrc && videoLoadError ? (
                <p className="muted">Video could not be loaded. The media server may be unreachable.</p>
              ) : null}
            </div>
          </aside>
        ) : null}
      </div>

    </section>
  ) : null

  if (!websiteObjectId) {
    return (
      <main className={`embed-shell${studioSurfaceClass}`} data-embed-theme={embedTheme} data-embed-surface={embedSurface} data-studio-theme={studioThemeAttr}>
        <ErrorState
          kind={routeErrorState?.kind || 'parse'}
          detail={routeErrorState?.detail || ''}
          kicker="Loci Embed"
          title="Invalid embed link"
          message="This published embed link cannot be opened as provided."
          className="embed-error-card"
          primaryActionLabel="Return to homepage"
          onPrimaryAction={() => navigateToPublicBrowse()}
        />
      </main>
    )
  }

  return (
    <main className={`embed-shell chrome-${chromeMode}${studioSurfaceClass}`.trim()} data-embed-theme={embedTheme} data-embed-surface={embedSurface} data-studio-theme={studioThemeAttr}>
      <div className={`embed-shell-inner ${compactChrome ? 'compact' : ''}`.trim()}>
        {!compactChrome ? (
          <header className="embed-shell-header">
            <div className="embed-shell-copy">
              <a
                className="surface-branding surface-branding-link embed-branding"
                href={PUBLIC_BROWSE_PATH}
                aria-label="Go to the public homepage"
                onClick={(event) => {
                  event.preventDefault()
                  navigateToPublicBrowse()
                }}
              >
                <BrandLockup variant="horizontal" />
                <p className="brand-tagline">Evidence, located.</p>
              </a>
              <h1>{manifest?.title || 'Loading object embed'}</h1>
              <p className="muted">{manifest?.summary || 'Preparing the published object and linked moments.'}</p>
            </div>
            <div className="embed-shell-meta">
              <span>{manifest?.annotations?.length || 0} moment{manifest?.annotations?.length === 1 ? '' : 's'}</span>
              <span>{manifest?.clips?.length || 0} clip{manifest?.clips?.length === 1 ? '' : 's'}</span>
            </div>
          </header>
        ) : null}

        {loading && !manifest ? (
          <section className="embed-loading-card" role="status" aria-live="polite">
            <strong>Loading object package...</strong>
            <p>Fetching the public manifest and preparing the 3D surface.</p>
          </section>
        ) : null}

        {errorState && !manifest ? (
          <ErrorState
            kind={errorState.kind}
            detail={errorState.detail}
            kicker="Loci Embed"
            title={errorState.kind === 'unauthorized' ? 'Embed is protected' : errorState.kind === 'not_found' ? 'Embed not found' : 'Embed unavailable'}
            message={errorState.kind === 'unauthorized' ? 'Access to this published embed requires authorization.' : errorState.kind === 'not_found' ? 'The published embed you requested could not be found.' : 'The published embed is unavailable right now.'}
            className="embed-error-card"
            primaryActionLabel={errorState.kind === 'unauthorized' ? 'Reload protected page' : errorState.kind === 'network' ? 'Try again' : 'Return to homepage'}
            onPrimaryAction={errorState.kind === 'unauthorized' ? reloadProtectedEmbedPage : errorState.kind === 'network' ? loadManifest : () => navigateToPublicBrowse()}
            secondaryActionLabel={errorState.kind === 'network' || errorState.kind === 'unauthorized' ? 'Return to homepage' : ''}
            onSecondaryAction={errorState.kind === 'network' || errorState.kind === 'unauthorized' ? () => navigateToPublicBrowse() : null}
          />
        ) : null}

        {manifest ? (
          <>
            {compactChrome ? compactStage : (
              <>
                <section className="embed-stage-card">
                  <div className="embed-stage-toolbar">
                    <div className="embed-stage-toolbar-group">
                      <button type="button" className="ghost" onClick={resetView}>Reset view</button>
                      <button type="button" className="ghost" onClick={toggleFullscreen}>Fullscreen</button>
                    </div>
                    {selectedAnnotation ? (
                      <span className="embed-stage-selection">Focused moment: {selectedAnnotation.label || selectedAnnotation.title}</span>
                    ) : (
                      <span className="embed-stage-selection">Select a moment to focus the model.</span>
                    )}
                  </div>

                  <div className="embed-stage" ref={stageRef}>
                    {shouldRenderModelCanvas ? (
                      <div className={`embed-stage-canvas ${modelReady ? 'ready' : 'pending'}`.trim()}>
                        <Suspense fallback={null}>
                          <LazyModelCanvas
                            modelUrl={embedModelUrl}
                            modelTransform={null}
                            defaultCameraView={appliedCameraView}
                            annotations={annotations}
                            selectedAnnotationId={selectedAnnotationId}
                            placementMode={false}
                            showAnnotationLabels={true}
                            showAutoLoadingOverlay={false}
                            backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
                            loadingEyebrow="Loci embed"
                            loadingTitle="Preparing interactive 3D view..."
                            loadingMessage="Loading model detail and linked moments."
                            emptyTitle="No 3D model"
                            emptyMessage="This embed currently has no interactive 3D asset."
                            cameraViewKey={`${cameraMode}:${cameraResetKey}:${selectedAnnotationId || 'none'}`}
                            onSceneReadyChange={handleSceneReadyChange}
                            onSelectAnnotation={(annotation) => setAnnotation(annotation.id, { autoplay: true, source: 'local' })}
                          />
                        </Suspense>
                      </div>
                    ) : null}

                    {manifest.modelSrc && (!modelReady || modelDeferredForMobile) ? (
                      <EmbedStageLoadingState
                        deferred={modelDeferredForMobile}
                        onAction={modelDeferredForMobile ? handleMobileModelLoadRequest : undefined}
                        highRisk={modelHighRisk}
                        backgroundColor={studioEnabled ? '#f3f5f9' : undefined}
                      />
                    ) : null}
                  </div>
                </section>

                <section className={`embed-detail-grid ${chromeMode}`.trim()}>
                  <article className="embed-panel embed-annotation-panel">
                    <div className="embed-panel-heading">
                      <strong>Moments</strong>
                      <span>{manifest.annotations.length} available</span>
                    </div>

                    <div className="embed-annotation-list">
                      {manifest.annotations.map((annotation) => {
                        const relatedClipCount = annotationRelatedClipIds(annotation).length
                        return (
                          <button
                            type="button"
                            key={annotation.id}
                            className={`embed-annotation-button ${annotation.id === selectedAnnotationId ? 'active' : ''}`.trim()}
                            onClick={() => setAnnotation(annotation.id, { autoplay: true, source: 'local' })}
                          >
                            <span>{annotation.label || annotation.title || annotation.id}</span>
                            {relatedClipCount ? <small>{relatedClipCount} linked clip{relatedClipCount === 1 ? '' : 's'}</small> : <small>View only</small>}
                          </button>
                        )
                      })}
                    </div>

                    {selectedAnnotation ? (
                      <div className="embed-annotation-copy">
                        <strong>{selectedAnnotation.title || selectedAnnotation.label}</strong>
                        {selectedAnnotation.body ? <p>{selectedAnnotation.body}</p> : null}
                        <div className="embed-annotation-actions">
                          {primaryRelatedClipId ? (
                            <button type="button" onClick={() => openClip(primaryRelatedClipId, { autoplay: true, source: 'local' })}>
                              Open linked clip
                            </button>
                          ) : null}
                          <button type="button" className="ghost" onClick={resetView}>Recenter model</button>
                        </div>

                        {showRelated && relationValues(selectedAnnotation).length ? (
                          <div className="embed-chip-row">
                            {relationValues(selectedAnnotation).map((value) => (
                              <span className="embed-chip" key={value}>{value}</span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    ) : (
                      <p className="muted">Choose a moment to focus the saved camera and open related clip context.</p>
                    )}
                  </article>

                  <article className="embed-panel embed-clip-panel">
                    <div className="embed-panel-heading">
                      <strong>Clip Playback</strong>
                      {activeClip ? <span>{activeClip.title}</span> : <span>No clip selected</span>}
                    </div>

                    {activeClip ? (
                      <div className="embed-clip-shell">
                        {selectedAnnotationPlaybackSequence.length > 1 ? (
                          <div className="embed-mini-playlist" aria-label="Related clips">
                            {selectedAnnotationPlaybackSequence.map((segment, index) => (
                              <button
                                type="button"
                                key={segment.key}
                                className={`embed-mini-clip-button ${index === activeSequenceIndex ? 'active' : ''}`.trim()}
                                onClick={() => selectedAnnotation && playAnnotationSegment(selectedAnnotation, index, { autoplay: true })}
                              >
                                {`Clip ${index + 1}`}
                              </button>
                            ))}
                          </div>
                        ) : null}
                        <video
                          key={activeClip.id}
                          ref={videoRef}
                          className="embed-clip-video"
                          src={activeClip.src}
                          controls
                          playsInline
                          preload="metadata"
                          onLoadedMetadata={() => {
                            setVideoLoadError(false)
                            if (!autoplayClip || !videoRef.current) {
                              return
                            }
                            videoRef.current.play().catch(() => null).finally(() => setAutoplayClip(false))
                          }}
                          onError={() => setVideoLoadError(true)}
                          onEnded={() => {
                            if (!sequencePlaybackActive || !selectedAnnotation) {
                              return
                            }

                            if (activeSequenceIndex + 1 < selectedAnnotationPlaybackSequence.length) {
                              playAnnotationSegment(selectedAnnotation, activeSequenceIndex + 1, { autoplay: true })
                              return
                            }

                            setSequencePlaybackActive(false)
                            setAutoplayClip(false)
                          }}
                        />
                        {videoLoadError ? (
                          <p className="muted">Video could not be loaded. The media server may be unreachable.</p>
                        ) : null}
                        <div className="embed-clip-copy">
                          <strong>{activeClip.title}</strong>
                          <span>{formatClipWindow(activeClip)}</span>
                          {activeClip.description ? <p>{activeClip.description}</p> : null}
                        </div>
                        {transcriptEnabled && activeClip.transcript ? (
                          <pre className="embed-clip-transcript">{activeClip.transcript}</pre>
                        ) : null}
                        <div className="embed-clip-actions">
                          <button type="button" className="ghost" onClick={closeClip}>Close clip</button>
                        </div>
                      </div>
                    ) : (
                      <div className="embed-clip-placeholder">
                        <strong>No clip open</strong>
                        <p>Select a moment with a linked public clip to open bounded playback inside the embed.</p>
                        <button type="button" className="ghost" onClick={focusFirstMoment}>
                          {firstInteractiveAnnotationId ? 'Focus first moment' : 'Reset view'}
                        </button>
                      </div>
                    )}
                  </article>
                </section>
              </>
            )}
          </>
        ) : null}
      </div>
    </main>
  )
}
