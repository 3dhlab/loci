import { useEffect, useRef, useState } from 'react'

import BrandMotion from './BrandMotion'
import ModelCanvas from './ModelCanvas'

const MOBILE_VIEWPORT_SCALE_EPSILON = 1.001
const MOBILE_VIEWPORT_HEIGHT_GAP = 24
const MOBILE_VIEWPORT_SETTLE_TIMEOUT_MS = 420
const MOBILE_LAYOUT_OVERFLOW_EPSILON = 1
const MOBILE_SEQUENCE_CUE_TIMEOUT_MS = 1400
const PUBLIC_PREVIEW_MAIN_CONTENT_ID = 'public-preview-main-content'

function buildForcedViewportContent(originalContent = 'width=device-width, initial-scale=1.0') {
  const normalizedParts = String(originalContent || 'width=device-width, initial-scale=1.0')
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .filter((part) => !/^(maximum-scale|minimum-scale|user-scalable)\s*=/.test(part))

  normalizedParts.push('maximum-scale=1')
  normalizedParts.push('user-scalable=no')
  return normalizedParts.join(', ')
}

function measureOverflowNode(label, node, viewportWidth) {
  if (!(node instanceof HTMLElement)) {
    return null
  }

  const clientWidth = Math.max(0, Number(node.clientWidth || 0))
  const scrollWidth = Math.max(clientWidth, Number(node.scrollWidth || 0))
  const rectWidth = Number((node.getBoundingClientRect?.().width || clientWidth).toFixed(2))
  const overflowPx = Math.max(0, scrollWidth - clientWidth, rectWidth - viewportWidth)

  return {
    label,
    clientWidth,
    scrollWidth,
    rectWidth,
    overflowPx: Number(overflowPx.toFixed(2))
  }
}

function formatAuthoritativeClipClock(ms, formatClock) {
  const normalizedMs = Number.isFinite(Number(ms)) ? Math.max(0, Number(ms)) : 0
  if (Math.round(normalizedMs) % 1000 === 0) {
    return formatClock(normalizedMs)
  }

  const totalSeconds = normalizedMs / 1000
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = (totalSeconds % 60).toFixed(1).padStart(4, '0')

  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${seconds}`
  }

  return `${String(minutes).padStart(2, '0')}:${seconds}`
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

function DrawerToggle({ active, children, onClick }) {
  return (
    <button
      type="button"
      className={`public-experience-drawer-toggle ${active ? 'active' : 'ghost'}`.trim()}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

function ActionEmptyState({ title, text, actionLabel, onAction }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p className="muted">{text}</p>
      {actionLabel && typeof onAction === 'function' ? (
        <button type="button" className="ghost" onClick={onAction}>{actionLabel}</button>
      ) : null}
    </div>
  )
}

export default function PublicExperienceShell({
  isPublicApp,
  publicSurfaceTitle,
  modelProjectId,
  setModelProjectId,
  modelObjectId,
  setModelObjectId,
  publishedProjects,
  filteredPublishedObjectsForPreview,
  openNowObjectIds,
  publicPreviewLoading,
  publicPreviewData,
  publicPreviewVideoId,
  setPublicPreviewVideoId,
  setPublicPreviewClipPlayback,
  setPublicPreviewPendingPlayback,
  setPublicPreviewCurrentMs,
  publicPreviewCurrentMs,
  publicPreviewViewerRef,
  publicPreviewVideoRef,
  applyPublicPreviewPendingPlayback,
  publicPreviewClipPlayback,
  publicPreviewSearchMoment,
  finishPublicPreviewClip,
  publicPreviewSelectedAnnotationPlaylist,
  publicPreviewClipIndex,
  playPublicPreviewAnnotationClip,
  publicPreviewTranscriptListRef,
  publicPreviewSegments,
  publicPreviewCurrentSegmentIndex,
  jumpToPublicPreviewMs,
  token,
  getObjectModelFileUrl,
  publicPreviewModelTransform,
  publicPreviewCameraView,
  publicPreviewSelectedAnnotation,
  publicPreviewClipVideo,
  publicPreviewClip,
  focusPublicPreviewAnnotation,
  openPublicPreviewEvidencePage,
  openPublicPreviewAnnotationEvidence,
  selectPublicPreviewAnnotation,
  searchForm,
  setSearchForm,
  runSegmentSearch,
  publishedVideos,
  searchLoading,
  searchResults,
  searchResultsSummary,
  renderSearchPagination,
  clearActiveSearchResults,
  semanticFitTier,
  formatSemanticFit,
  formatClock,
  sortedVideos,
  publishedObjectIds,
  openPublicPreviewResult,
  setPublicPreviewSearchMoment,
  focusEvidenceSignal,
}) {
  const [drawerOpen, setDrawerOpen] = useState(true)
  const [drawerView, setDrawerView] = useState('evidence')
  const [isMobileShell, setIsMobileShell] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia('(max-width: 760px)').matches : false
  )
  const [mobilePrimaryMode, setMobilePrimaryMode] = useState('object')
  const [mobileEvidenceOpen, setMobileEvidenceOpen] = useState(false)
  const [mobileVideoIsPlaying, setMobileVideoIsPlaying] = useState(false)
  const [mobileVideoDurationMs, setMobileVideoDurationMs] = useState(0)
  const [mobileViewportResetPending, setMobileViewportResetPending] = useState(false)
  const [mobileClipTransitionCue, setMobileClipTransitionCue] = useState('')
  const [mobileClipTransitionBusy, setMobileClipTransitionBusy] = useState(false)
  const shellRef = useRef(null)
  const searchInputRef = useRef(null)
  const clipBoundaryFrameRef = useRef(0)
  const clipBoundaryVideoFrameRef = useRef(0)
  const clipBoundaryAdvanceRef = useRef(0)
  const clipBoundaryFinishedRef = useRef(false)
  const mobileScrollLockYRef = useRef(0)
  const previousClipIndexRef = useRef(0)
  const mobileClipTransitionTimeoutRef = useRef(0)
  const mobileViewportDiagnosticsRef = useRef(null)

  const activeProject = publishedProjects.find((project) => project.id === publicPreviewData?.object?.project_id) || null

  function dismissMobileInteractiveFocus() {
    if (!isMobileShell || typeof document === 'undefined') {
      return
    }

    const activeElement = document.activeElement
    if (!(activeElement instanceof HTMLElement)) {
      return
    }

    const tagName = activeElement.tagName.toLowerCase()
    if (tagName === 'input' || tagName === 'textarea' || tagName === 'select' || activeElement.isContentEditable) {
      activeElement.blur()
    }
  }

  function hasFocusedMobileTextControl() {
    if (typeof document === 'undefined') {
      return false
    }

    const activeElement = document.activeElement
    if (!(activeElement instanceof HTMLElement)) {
      return false
    }

    const tagName = activeElement.tagName.toLowerCase()
    return tagName === 'input' || tagName === 'textarea' || activeElement.isContentEditable
  }

  function readMobileLayoutOverflowState() {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
      return {
        viewportWidth: 0,
        hasOverflow: false,
        nodes: [],
        overflowingNodes: []
      }
    }

    const viewportWidth = Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0)
    const candidates = [
      ['document', document.documentElement],
      ['body', document.body],
      ['app-shell', document.querySelector('.app-shell.preview-route')],
      ['public-shell', shellRef.current],
      ['mobile-layout', shellRef.current?.querySelector('.public-experience-mobile-layout')],
      ['mobile-object-stack', shellRef.current?.querySelector('.public-experience-mobile-object-stack')],
      ['model-card', shellRef.current?.querySelector('.public-experience-model-card')],
      ['model-stage', shellRef.current?.querySelector('.public-experience-model-card .model-canvas-shell')],
      ['moment-strip', shellRef.current?.querySelector('.public-experience-mobile-moment-strip')],
      ['moment-copy', shellRef.current?.querySelector('.public-experience-mobile-moment-copy')],
      ['moment-body', shellRef.current?.querySelector('.public-experience-moment-body')],
      ['notice-banner', document.querySelector('.app-shell.preview-route .notice')]
    ]

    const nodes = candidates
      .map(([label, node]) => measureOverflowNode(label, node, viewportWidth))
      .filter(Boolean)

    const overflowingNodes = nodes.filter((entry) => entry.overflowPx > MOBILE_LAYOUT_OVERFLOW_EPSILON)

    return {
      viewportWidth,
      hasOverflow: overflowingNodes.length > 0,
      nodes,
      overflowingNodes
    }
  }

  function publishMobileViewportDiagnostics(label) {
    const snapshot = {
      label,
      ...readMobileViewportState()
    }

    mobileViewportDiagnosticsRef.current = snapshot
    if (shellRef.current) {
      shellRef.current.dataset.mobileOverflowState = snapshot.layoutOverflow.hasOverflow
        ? 'overflowing'
        : snapshot.needsSettle
          ? 'settling'
          : 'settled'
    }
    if (typeof window !== 'undefined') {
      window.__semanticPublicPreviewMobileDiagnostics = snapshot
    }
    return snapshot
  }

  function readMobileViewportState() {
    if (typeof window === 'undefined') {
      return {
        viewport: null,
        baselineHeight: 0,
        heightGap: 0,
        scale: 1,
        offsetTop: 0,
        documentWidth: 0,
        documentScrollWidth: 0,
        layoutOverflow: {
          viewportWidth: 0,
          hasOverflow: false,
          nodes: [],
          overflowingNodes: []
        },
        needsSettle: false
      }
    }

    const viewport = window.visualViewport || null
    const documentWidth = Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0)
    const documentScrollWidth = Math.max(documentWidth, document.documentElement?.scrollWidth || 0)
    const baselineHeight = Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0)
    const heightGap = viewport && baselineHeight > 0 ? Math.max(0, baselineHeight - viewport.height) : 0
    const scale = viewport ? Number(viewport.scale || 1) : 1
    const offsetTop = viewport ? Math.abs(Number(viewport.offsetTop || 0)) : 0
    const layoutOverflow = readMobileLayoutOverflowState()

    return {
      viewport,
      baselineHeight,
      heightGap,
      scale,
      offsetTop,
      documentWidth,
      documentScrollWidth,
      layoutOverflow,
      needsSettle: hasFocusedMobileTextControl()
        || scale > MOBILE_VIEWPORT_SCALE_EPSILON
        || offsetTop > 0.5
        || heightGap > MOBILE_VIEWPORT_HEIGHT_GAP
        || layoutOverflow.hasOverflow
    }
  }

  async function nextAnimationFrames(frameCount = 2) {
    if (typeof window === 'undefined') {
      return
    }

    let remainingFrames = Math.max(0, frameCount)
    await new Promise((resolve) => {
      const step = () => {
        if (remainingFrames <= 0) {
          resolve()
          return
        }
        remainingFrames -= 1
        window.requestAnimationFrame(step)
      }

      window.requestAnimationFrame(step)
    })
  }

  async function waitForMobileViewportReset() {
    if (!isMobileShell || typeof window === 'undefined') {
      return
    }

    const initialState = readMobileViewportState()
    const { viewport } = initialState
    if (!initialState.needsSettle) {
      publishMobileViewportDiagnostics('already-settled')
      return
    }

    await new Promise((resolve) => {
      let resolved = false
      let frameId = 0
      let timeoutId = 0
      let stableFrames = 0

      const finish = (label = 'settled') => {
        if (resolved) {
          return
        }
        resolved = true
        if (frameId) {
          window.cancelAnimationFrame(frameId)
        }
        if (timeoutId) {
          window.clearTimeout(timeoutId)
        }
        window.removeEventListener('resize', scheduleCheck)
        viewport?.removeEventListener('resize', scheduleCheck)
        viewport?.removeEventListener('scroll', scheduleCheck)
        publishMobileViewportDiagnostics(label)
        resolve()
      }

      const check = () => {
        if (!readMobileViewportState().needsSettle) {
          stableFrames += 1
          if (stableFrames >= 2) {
            finish('settled')
            return
          }
        } else {
          stableFrames = 0
        }
        frameId = window.requestAnimationFrame(check)
      }

      const scheduleCheck = () => {
        if (resolved) {
          return
        }
        if (frameId) {
          window.cancelAnimationFrame(frameId)
        }
        frameId = window.requestAnimationFrame(check)
      }

      timeoutId = window.setTimeout(() => finish('timeout'), MOBILE_VIEWPORT_SETTLE_TIMEOUT_MS)
      window.addEventListener('resize', scheduleCheck)
      viewport?.addEventListener('resize', scheduleCheck)
      viewport?.addEventListener('scroll', scheduleCheck)
      scheduleCheck()
    })
  }

  async function forceMobileViewportReset() {
    if (!isMobileShell || typeof document === 'undefined') {
      return false
    }

    const viewportMeta = document.querySelector('meta[name="viewport"]')
    if (!viewportMeta) {
      await nextAnimationFrames()
      return false
    }

    const originalContent = viewportMeta.getAttribute('content') || 'width=device-width, initial-scale=1.0'
    viewportMeta.setAttribute('content', buildForcedViewportContent(originalContent))
    await nextAnimationFrames()
    viewportMeta.setAttribute('content', originalContent)
    await nextAnimationFrames()
    return true
  }

  async function normalizeMobileViewport({ restoreScrollTop = null } = {}) {
    if (!isMobileShell || typeof window === 'undefined') {
      return
    }

    publishMobileViewportDiagnostics('before-normalize')
    dismissMobileInteractiveFocus()

    if (Number.isFinite(restoreScrollTop)) {
      window.scrollTo(0, Math.max(0, Number(restoreScrollTop || 0)))
    } else {
      window.scrollTo(0, Math.max(0, window.scrollY || window.pageYOffset || 0))
    }
    document.documentElement.scrollLeft = 0
    document.body.scrollLeft = 0

    await waitForMobileViewportReset()
    if (!readMobileViewportState().needsSettle) {
      window.dispatchEvent(new Event('resize'))
      await nextAnimationFrames(2)
      publishMobileViewportDiagnostics('after-normalize')
      return
    }

    const forcedResetApplied = await forceMobileViewportReset()
    if (forcedResetApplied && Number.isFinite(restoreScrollTop)) {
      window.scrollTo(0, Math.max(0, Number(restoreScrollTop || 0)))
    }
    document.documentElement.scrollLeft = 0
    document.body.scrollLeft = 0
    window.dispatchEvent(new Event('resize'))
    await nextAnimationFrames(3)
    await waitForMobileViewportReset()
    publishMobileViewportDiagnostics(forcedResetApplied ? 'after-forced-reset' : 'after-reset-wait')
  }

  function closeMobileEvidenceOverlay({ normalizeViewport = true } = {}) {
    dismissMobileInteractiveFocus()
    if (normalizeViewport && isMobileShell && mobileEvidenceOpen) {
      setMobileViewportResetPending(true)
    }
    setMobileEvidenceOpen(false)
    setMobilePrimaryMode('object')
  }

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined
    }

    const mediaQuery = window.matchMedia('(max-width: 760px)')
    const updateMobileShell = (event) => setIsMobileShell(event.matches)

    setIsMobileShell(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', updateMobileShell)
      return () => mediaQuery.removeEventListener('change', updateMobileShell)
    }

    mediaQuery.addListener(updateMobileShell)
    return () => mediaQuery.removeListener(updateMobileShell)
  }, [])

  useEffect(() => {
    if (searchResults.length) {
      setDrawerOpen(true)
      setDrawerView('search')
      if (isMobileShell) {
        setMobilePrimaryMode('search')
      }
    }
  }, [isMobileShell, searchResults.length])

  useEffect(() => {
    if (!focusEvidenceSignal) {
      return
    }
    setDrawerOpen(true)
    setDrawerView('evidence')
    if (isMobileShell) {
      setMobileEvidenceOpen(true)
    }
  }, [focusEvidenceSignal, isMobileShell])

  useEffect(() => {
    const hasSearchMomentForCurrentObject = Boolean(
      publicPreviewSearchMoment && publicPreviewSearchMoment.objectId === publicPreviewData?.object?.id
    )

    if (!publicPreviewClipPlayback && !hasSearchMomentForCurrentObject) {
      return
    }

    setDrawerOpen(true)
    setDrawerView('evidence')
    if (isMobileShell) {
      setMobilePrimaryMode('object')
      setMobileEvidenceOpen(true)
    }
  }, [
    isMobileShell,
    publicPreviewClipPlayback,
    publicPreviewData?.object?.id,
    publicPreviewSearchMoment,
    publicPreviewSelectedAnnotation?.id
  ])

  useEffect(() => {
    if (!isMobileShell) {
      return
    }
    setMobilePrimaryMode('object')
    if (publicPreviewClipPlayback || publicPreviewSearchMoment) {
      return
    }
    setMobileEvidenceOpen(false)
  }, [isMobileShell, publicPreviewClipPlayback, publicPreviewSearchMoment, publicPreviewData?.object?.id])

  useEffect(() => {
    setMobileVideoIsPlaying(false)
    setMobileVideoDurationMs(0)
  }, [publicPreviewData?.video?.id, publicPreviewClipPlayback?.videoId, publicPreviewClipPlayback?.startMs, publicPreviewClipPlayback?.endMs])

  useEffect(() => {
    if (!isMobileShell || mobilePrimaryMode !== 'search' || !searchInputRef.current) {
      return
    }

    window.requestAnimationFrame(() => {
      searchInputRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    })
  }, [isMobileShell, mobilePrimaryMode])

  useEffect(() => {
    return () => {
      if (mobileClipTransitionTimeoutRef.current) {
        window.clearTimeout(mobileClipTransitionTimeoutRef.current)
        mobileClipTransitionTimeoutRef.current = 0
      }
    }
  }, [])

  useEffect(() => {
    if (!isMobileShell || typeof document === 'undefined') {
      return undefined
    }

    const shouldLock = Boolean(mobileEvidenceOpen)
    if (!shouldLock) {
      return undefined
    }

    dismissMobileInteractiveFocus()

    mobileScrollLockYRef.current = window.scrollY || window.pageYOffset || 0
    const previousOverflow = document.body.style.overflow
    const previousBodyPosition = document.body.style.position
    const previousBodyTop = document.body.style.top
    const previousBodyWidth = document.body.style.width
    const previousBodyLeft = document.body.style.left
    const previousBodyRight = document.body.style.right
    const previousDocumentOverflow = document.documentElement.style.overflow
    document.body.style.overflow = 'hidden'
    document.body.style.position = 'fixed'
    document.body.style.top = `-${mobileScrollLockYRef.current}px`
    document.body.style.left = '0'
    document.body.style.right = '0'
    document.body.style.width = '100%'
    document.documentElement.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
      document.body.style.position = previousBodyPosition
      document.body.style.top = previousBodyTop
      document.body.style.width = previousBodyWidth
      document.body.style.left = previousBodyLeft
      document.body.style.right = previousBodyRight
      document.documentElement.style.overflow = previousDocumentOverflow
      window.scrollTo(0, mobileScrollLockYRef.current)
    }
  }, [isMobileShell, mobileEvidenceOpen])

  useEffect(() => {
    if (!mobileEvidenceOpen || !mobileViewportResetPending) {
      return
    }

    setMobileViewportResetPending(false)
  }, [mobileEvidenceOpen, mobileViewportResetPending])

  useEffect(() => {
    if (!isMobileShell || mobileEvidenceOpen || !mobileViewportResetPending) {
      return undefined
    }

    let cancelled = false

    async function resetViewportAfterOverlayClose() {
      try {
        await normalizeMobileViewport({ restoreScrollTop: mobileScrollLockYRef.current })
      } finally {
        if (!cancelled) {
          setMobileViewportResetPending(false)
        }
      }
    }

    resetViewportAfterOverlayClose()
    return () => {
      cancelled = true
    }
  }, [isMobileShell, mobileEvidenceOpen, mobileViewportResetPending])

  useEffect(() => {
    if (!isMobileShell) {
      previousClipIndexRef.current = publicPreviewClipIndex
      setMobileClipTransitionCue('')
      setMobileClipTransitionBusy(false)
      return
    }

    const totalClips = publicPreviewSelectedAnnotationPlaylist.length
    if (!publicPreviewSelectedAnnotation || totalClips < 2) {
      previousClipIndexRef.current = publicPreviewClipIndex
      setMobileClipTransitionCue('')
      setMobileClipTransitionBusy(false)
      return
    }

    const previousClipIndex = previousClipIndexRef.current
    previousClipIndexRef.current = publicPreviewClipIndex
    if (previousClipIndex === publicPreviewClipIndex) {
      return
    }

    setMobileClipTransitionCue(`Clip ${publicPreviewClipIndex + 1} of ${totalClips}`)
    if (mobileClipTransitionTimeoutRef.current) {
      window.clearTimeout(mobileClipTransitionTimeoutRef.current)
    }
    mobileClipTransitionTimeoutRef.current = window.setTimeout(() => {
      setMobileClipTransitionCue('')
      mobileClipTransitionTimeoutRef.current = 0
    }, MOBILE_SEQUENCE_CUE_TIMEOUT_MS)
  }, [isMobileShell, publicPreviewClipIndex, publicPreviewSelectedAnnotation, publicPreviewSelectedAnnotationPlaylist.length])

  useEffect(() => {
    if (!isMobileShell || !mobileEvidenceOpen || !publicPreviewTranscriptListRef.current || publicPreviewCurrentSegmentIndex < 0) {
      return
    }

    const container = publicPreviewTranscriptListRef.current
    const target = container.querySelector(
      `[data-public-preview-segment-index="${publicPreviewCurrentSegmentIndex}"]`
    )
    if (!target) {
      return
    }

    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const nextScrollTop = Math.max(
          0,
          target.offsetTop - ((container.clientHeight - target.clientHeight) / 2)
        )
        container.scrollTo({ top: nextScrollTop, behavior: 'smooth' })
      })
    })
  }, [
    isMobileShell,
    mobileEvidenceOpen,
    publicPreviewClipIndex,
    publicPreviewCurrentSegmentIndex,
    publicPreviewSegments.length,
    publicPreviewSelectedAnnotation?.id
  ])

  useEffect(() => {
    if (!publicPreviewClipPlayback || publicPreviewClipPlayback.videoId !== publicPreviewData?.video?.id) {
      return undefined
    }

    const media = publicPreviewVideoRef.current
    if (!media) {
      return undefined
    }

    clipBoundaryFinishedRef.current = false
    const clipEndMs = Math.max(0, Number(publicPreviewClipPlayback.endMs || 0))

    const finishAtBoundary = () => {
      if (!media || clipBoundaryFinishedRef.current) {
        return
      }

      clipBoundaryFinishedRef.current = true
      media.pause()
      media.currentTime = clipEndMs / 1000
      setPublicPreviewCurrentMs(clipEndMs)
      clipBoundaryAdvanceRef.current = window.requestAnimationFrame(() => {
        finishPublicPreviewClip()
      })
    }

    const monitorClipBoundary = () => {
      if (!media || clipBoundaryFinishedRef.current) {
        return
      }

      if (!media.paused && !media.ended) {
        const currentMs = Number(media.currentTime || 0) * 1000
        if (clipEndMs > 0 && currentMs >= clipEndMs) {
          finishAtBoundary()
          return
        }
      }

      clipBoundaryFrameRef.current = window.requestAnimationFrame(monitorClipBoundary)
    }

    const monitorRenderedFrame = (_now, metadata) => {
      if (!media || clipBoundaryFinishedRef.current) {
        return
      }

      if (!media.paused && !media.ended) {
        const currentMs = Number(metadata?.mediaTime ?? media.currentTime ?? 0) * 1000
        if (clipEndMs > 0 && currentMs >= clipEndMs) {
          finishAtBoundary()
          return
        }
      }

      if (typeof media.requestVideoFrameCallback === 'function') {
        clipBoundaryVideoFrameRef.current = media.requestVideoFrameCallback(monitorRenderedFrame)
      }
    }

    if (typeof media.requestVideoFrameCallback === 'function') {
      clipBoundaryVideoFrameRef.current = media.requestVideoFrameCallback(monitorRenderedFrame)
    } else {
      clipBoundaryFrameRef.current = window.requestAnimationFrame(monitorClipBoundary)
    }

    return () => {
      clipBoundaryFinishedRef.current = false
      if (clipBoundaryFrameRef.current) {
        window.cancelAnimationFrame(clipBoundaryFrameRef.current)
        clipBoundaryFrameRef.current = 0
      }
      if (clipBoundaryAdvanceRef.current) {
        window.cancelAnimationFrame(clipBoundaryAdvanceRef.current)
        clipBoundaryAdvanceRef.current = 0
      }
      if (clipBoundaryVideoFrameRef.current && typeof media.cancelVideoFrameCallback === 'function') {
        media.cancelVideoFrameCallback(clipBoundaryVideoFrameRef.current)
        clipBoundaryVideoFrameRef.current = 0
      }
    }
  }, [
    finishPublicPreviewClip,
    publicPreviewClipPlayback,
    publicPreviewData?.video?.id,
    publicPreviewVideoRef,
    setPublicPreviewCurrentMs
  ])

  const objectSummary = publicPreviewData?.object?.public_standfirst
    || publicPreviewData?.object?.description
    || 'Open the evidence view to follow recorded moments, transcript lines, and 3D context for this object.'

  const activeSearchMoment = publicPreviewSearchMoment && publicPreviewSearchMoment.objectId === publicPreviewData?.object?.id
    ? publicPreviewSearchMoment
    : null
  const deferSearchMomentModel = isMobileShell && Boolean(activeSearchMoment) && (mobileEvidenceOpen || mobileViewportResetPending)
  const activeClipPlayback = publicPreviewClipPlayback && publicPreviewData?.video?.id === publicPreviewClipPlayback.videoId
    ? {
        startMs: Math.max(0, Math.floor(Number(publicPreviewClipPlayback.startMs || 0))),
        endMs: Math.max(
          Math.max(0, Math.floor(Number(publicPreviewClipPlayback.startMs || 0))),
          Math.floor(Number(publicPreviewClipPlayback.endMs ?? publicPreviewClipPlayback.startMs ?? 0))
        )
      }
    : null
  const mobilePlaybackStartMs = activeClipPlayback ? activeClipPlayback.startMs : 0
  const mobilePlaybackEndMs = activeClipPlayback
    ? activeClipPlayback.endMs
    : Math.max(0, Math.floor(Number(mobileVideoDurationMs || 0)))
  const mobilePlaybackDurationMs = Math.max(0, mobilePlaybackEndMs - mobilePlaybackStartMs)
  const mobilePlaybackProgressMs = activeClipPlayback
    ? Math.max(0, Math.min(mobilePlaybackDurationMs, Math.floor(Number(publicPreviewCurrentMs || 0)) - mobilePlaybackStartMs))
    : Math.max(0, Math.min(Math.floor(Number(publicPreviewCurrentMs || 0)), mobilePlaybackEndMs || Math.floor(Number(publicPreviewCurrentMs || 0))))
  const formatClipClock = (valueMs) => formatAuthoritativeClipClock(valueMs, formatClock)
  const activeMomentRangeStartMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.start_ms ?? publicPreviewSelectedAnnotation?.start_ms)
    : (activeSearchMoment?.contextStartMs ?? activeSearchMoment?.startMs)
  const activeMomentRangeEndMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.end_ms ?? publicPreviewSelectedAnnotation?.end_ms)
    : (activeSearchMoment?.contextEndMs ?? activeSearchMoment?.endMs)
  const activePlaybackRangeStartMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.start_ms ?? publicPreviewSelectedAnnotation?.start_ms)
    : (activeSearchMoment?.contextStartMs ?? activeSearchMoment?.startMs)
  const activePlaybackRangeEndMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.end_ms ?? publicPreviewSelectedAnnotation?.end_ms)
    : (activeSearchMoment?.contextEndMs ?? activeSearchMoment?.endMs)
  const activeMatchedRangeStartMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.start_ms ?? publicPreviewSelectedAnnotation?.start_ms)
    : (activeSearchMoment?.matchedStartMs ?? activeSearchMoment?.startMs)
  const activeMatchedRangeEndMs = publicPreviewSelectedAnnotation
    ? (publicPreviewClip?.end_ms ?? publicPreviewSelectedAnnotation?.end_ms)
    : (activeSearchMoment?.matchedEndMs ?? activeSearchMoment?.endMs)

  const hasEvidencePage = Boolean(publicPreviewData?.object?.evidence_url)
  const hasMomentPage = Boolean(publicPreviewSelectedAnnotation?.evidence_url)
  const currentMomentActionLabel = hasMomentPage ? 'Open moment page' : (hasEvidencePage ? 'Open evidence page' : 'Open moment')
  const currentMomentTitle = publicPreviewSelectedAnnotation?.title || activeSearchMoment?.title || 'Search result'
  const currentMomentVideoLabel = publicPreviewSelectedAnnotation
    ? (publicPreviewClipVideo?.title || publicPreviewData?.videos.find((video) => video.id === publicPreviewSelectedAnnotation.video_id)?.title || 'Linked video unavailable')
    : (activeSearchMoment?.videoTitle || publicPreviewData?.video?.title || 'Linked video unavailable')
  const currentMomentSummary = publicPreviewSelectedAnnotation?.description
    || activeSearchMoment?.sceneDescription
    || activeSearchMoment?.description
    || 'Open the linked video and transcript to continue through this moment.'
  const showMatchedExcerptNote = !publicPreviewSelectedAnnotation
    && Boolean(activeSearchMoment)
    && activeSearchMoment.contextStartMs !== activeSearchMoment.matchedStartMs
  const evidenceCount = publicPreviewData?.annotations?.length || 0
  const videoCount = publicPreviewData?.videos?.length || 0
  const evidenceSummary = `${evidenceCount} interpretation${evidenceCount === 1 ? '' : 's'} · ${videoCount} evidence video${videoCount === 1 ? '' : 's'}`
  const loadingMotionName = publicPreviewData ? 'scan' : 'assemble'
  const loadingTitle = publicPreviewData
    ? `Updating ${publicSurfaceTitle.toLowerCase()}...`
    : `Loading ${publicSurfaceTitle.toLowerCase()}...`
  const loadingMessage = publicPreviewData
    ? 'Keeping the current object stage visible while the next published package loads.'
    : 'Preparing the object shell, model stage, and linked evidence for this selection.'

  const loadingBanner = publicPreviewLoading ? (
    <section
      className={`public-experience-loading-banner ${publicPreviewData ? 'overlaying' : 'initial'}`.trim()}
      role="status"
      aria-live="polite"
    >
      <div className="public-experience-loading-mark" aria-hidden="true">
        <BrandMotion name={loadingMotionName} size={publicPreviewData ? 44 : 56} className="brand-motion-glow" />
      </div>
      <div className="public-experience-loading-copy">
        <span className="public-experience-eyebrow">{isPublicApp ? 'Live database' : 'Public preview'}</span>
        <strong>{loadingTitle}</strong>
        <p>{loadingMessage}</p>
      </div>
      <div className="public-experience-loading-pulse" aria-hidden="true">
        <span />
      </div>
    </section>
  ) : null

  const loadingShell = (
    <section className="public-experience-loading-shell" role="status" aria-live="polite">
      <div className="public-experience-loading-shell-header">
        <div className="public-experience-loading-shell-mark" aria-hidden="true">
          <BrandMotion name="assemble" size={96} className="brand-motion-glow" />
        </div>
        <div className="public-experience-loading-shell-copy">
          <span className="public-experience-eyebrow">{isPublicApp ? 'Published object package' : 'Preview package'}</span>
          <h2>{loadingTitle}</h2>
          <p>{loadingMessage}</p>
        </div>
      </div>
      <div className="public-experience-loading-shell-grid" aria-hidden="true">
        <div className="public-experience-loading-block stage">
          <div className="public-experience-loading-stage-mark">
            <BrandMotion name="pivot" size={124} className="brand-motion-glow" />
          </div>
        </div>
        <div className="public-experience-loading-block detail" />
        <div className="public-experience-loading-block detail" />
      </div>
    </section>
  )

  function activateEvidenceView() {
    dismissMobileInteractiveFocus()

    if (openPublicPreviewEvidencePage?.()) {
      return
    }
    setDrawerOpen(true)
    setDrawerView('evidence')
    if (isMobileShell) {
      if (publicPreviewData?.video?.id && Number.isFinite(publicPreviewCurrentMs) && publicPreviewCurrentMs > 0) {
        setPublicPreviewPendingPlayback({
          videoId: publicPreviewData.video.id,
          seekMs: publicPreviewCurrentMs,
          autoplay: false
        })
      }
      setMobileViewportResetPending(false)
      setMobileEvidenceOpen(true)
    }
  }

  function activateMomentPage() {
    if (openPublicPreviewAnnotationEvidence?.(publicPreviewSelectedAnnotation || null)) {
      return
    }

    activateEvidenceView()
  }

  function activateSearchView() {
    setDrawerOpen(true)
    setDrawerView('search')
    if (isMobileShell) {
      setMobileEvidenceOpen(false)
      setMobilePrimaryMode('search')
    }
  }

  function activateObjectView() {
    if (!isMobileShell) {
      return
    }

    closeMobileEvidenceOverlay()
  }

  function focusShellControl(controlId) {
    if (typeof document === 'undefined') {
      return
    }

    const control = document.getElementById(controlId)
    if (!(control instanceof HTMLElement)) {
      return
    }

    control.focus()
    control.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' })
  }

  function focusObjectSelector() {
    activateObjectView()
    window.requestAnimationFrame(() => focusShellControl('public-preview-object-select'))
  }

  function focusVideoSelector() {
    activateObjectView()
    window.requestAnimationFrame(() => focusShellControl('public-preview-video-select'))
  }

  function focusSearchField() {
    activateSearchView()
    window.requestAnimationFrame(() => {
      const target = searchInputRef.current
      if (!target) {
        return
      }

      target.focus()
      target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' })
    })
  }

  const canChooseAnotherObject = filteredPublishedObjectsForPreview.length > 1
  const canChooseAnotherVideo = Number(publicPreviewData?.videos?.length || 0) > 1
  const primaryObjectActionLabel = canChooseAnotherObject
    ? 'Choose another object'
    : hasEvidencePage
      ? 'Open evidence page'
      : 'Search moments'
  const primaryObjectAction = canChooseAnotherObject
    ? focusObjectSelector
    : hasEvidencePage
      ? activateEvidenceView
      : focusSearchField

  function handleAnnotationSelect(annotation) {
    if (!annotation) {
      return
    }

    if (isMobileShell) {
      dismissMobileInteractiveFocus()
      setDrawerOpen(true)
      setDrawerView('evidence')
      setMobileViewportResetPending(false)
      setMobileEvidenceOpen(true)
      playPublicPreviewAnnotationClip(annotation, 0)
      return
    }

    selectPublicPreviewAnnotation(annotation)
  }

  async function handleSearchResultSelect(result) {
    if (isMobileShell) {
      dismissMobileInteractiveFocus()
      setDrawerOpen(true)
      setDrawerView('evidence')
      setMobilePrimaryMode('object')
      setMobileViewportResetPending(false)
      await normalizeMobileViewport()
    }

    openPublicPreviewResult(result)
  }

  function humanizeMediaTitle(value = '') {
    const normalized = String(value || '').trim()
    if (!normalized) {
      return ''
    }

    const withoutExtension = normalized.replace(/\.[^.]+$/, '')
    const withoutTechnicalTokens = withoutExtension
      .replace(/(^|[\s_-])(semantic\d*k|gdfinal|gd\s*final|final|normalized)(?=$|[\s_-])/gi, ' ')
      .replace(/(^|[\s_-])[0-9a-f]{6,}(?=$|[\s_-])/gi, ' ')
    const withSpaces = withoutTechnicalTokens
      .replace(/[_-]+/g, ' ')
      .replace(/([a-z\d])([A-Z])/g, '$1 $2')
      .replace(/([A-Za-z])(\d)/g, '$1 $2')
      .replace(/(\d)([A-Za-z])/g, '$1 $2')
      .replace(/\s+/g, ' ')
      .trim()

    return withSpaces || withoutTechnicalTokens || withoutExtension || normalized
  }

  function restartMobilePlayback() {
    jumpToPublicPreviewMs(mobilePlaybackStartMs, true, {
      preserveClipPlayback: Boolean(activeClipPlayback)
    })
  }

  function toggleMobilePlayback() {
    const media = publicPreviewVideoRef.current
    if (!media) {
      restartMobilePlayback()
      return
    }

    const currentMs = Math.max(0, Math.floor((media.currentTime || 0) * 1000))
    if (media.paused || media.ended) {
      if (activeClipPlayback && currentMs >= (mobilePlaybackEndMs - 250)) {
        restartMobilePlayback()
        return
      }
      media.play().catch(() => null)
      return
    }

    media.pause()
  }

  function scrubMobilePlayback(nextRelativeMs) {
    const nextMs = activeClipPlayback
      ? mobilePlaybackStartMs + Math.max(0, Math.floor(Number(nextRelativeMs || 0)))
      : Math.max(0, Math.floor(Number(nextRelativeMs || 0)))

    jumpToPublicPreviewMs(nextMs, false, {
      preserveClipPlayback: Boolean(activeClipPlayback)
    })
  }

  const mobileInlineControls = isMobileShell && publicPreviewData?.videoUrl ? (
    <div className="public-experience-mobile-video-controls">
      <div className="public-experience-mobile-video-actions">
        <button type="button" onClick={toggleMobilePlayback}>
          {mobileVideoIsPlaying ? (activeClipPlayback ? 'Pause clip' : 'Pause video') : (activeClipPlayback ? 'Play clip' : 'Play video')}
        </button>
        <button type="button" className="ghost" onClick={restartMobilePlayback}>
          {activeClipPlayback ? 'Restart clip' : 'Restart video'}
        </button>
      </div>

      <label className="public-experience-mobile-video-timeline">
        <span className="public-experience-mobile-video-status">
          <strong>{activeClipPlayback ? 'Clip playback' : 'Video playback'}</strong>
          <span>{formatClipClock(mobilePlaybackProgressMs)} / {formatClipClock(mobilePlaybackDurationMs)}</span>
        </span>
        <input
          type="range"
          min="0"
          max={Math.max(1, mobilePlaybackDurationMs)}
          step="250"
          value={Math.min(Math.max(0, mobilePlaybackProgressMs), Math.max(1, mobilePlaybackDurationMs))}
          onInput={(event) => scrubMobilePlayback(event.currentTarget.value)}
          aria-label={activeClipPlayback ? 'Clip playback timeline' : 'Video playback timeline'}
        />
      </label>
    </div>
  ) : null

  const evidencePlaylistBar = publicPreviewSelectedAnnotationPlaylist.length > 1 ? (
    <div className="annotation-overlay-playlist-bar public-preview-playlist-bar">
      {publicPreviewSelectedAnnotationPlaylist.map((entry, index) => (
        <button
          type="button"
          key={`${entry.video_id}-${entry.start_ms}-${entry.end_ms}-${index}`}
          className={publicPreviewClipIndex === index ? 'annotation-playlist-chip active' : 'annotation-playlist-chip'}
          onClick={() => playPublicPreviewAnnotationClip(publicPreviewSelectedAnnotation, index)}
        >
          {entry.label || `Clip ${index + 1}`}
        </button>
      ))}
    </div>
  ) : null

  const searchStack = (
    <div className="public-experience-search-stack">
      <div className="public-experience-section-heading">
        <h3>Search</h3>
        <p className="muted">Search across published transcript evidence and open directly into the synchronized object moment.</p>
      </div>

      <form className="stack" onSubmit={runSegmentSearch}>
        <input
          ref={searchInputRef}
          placeholder="Search transcript text"
          value={searchForm.query}
          onChange={(event) => setSearchForm((prev) => ({ ...prev, query: event.target.value }))}
        />
        <div className="row">
          <select
            value={searchForm.project_id}
            onChange={(event) => setSearchForm((prev) => ({ ...prev, project_id: event.target.value, video_id: '' }))}
          >
            <option value="">All projects</option>
            {publishedProjects.map((project) => (
              <option value={project.id} key={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <select
            value={searchForm.video_id}
            onChange={(event) => setSearchForm((prev) => ({ ...prev, video_id: event.target.value }))}
          >
            <option value="">All videos</option>
            {publishedVideos
              .filter((video) => !searchForm.project_id || video.project_id === searchForm.project_id)
              .map((video) => (
                <option value={video.id} key={video.id}>
                  {video.title}
                </option>
              ))}
          </select>
        </div>
        <button type="submit" disabled={searchLoading}>
          {searchLoading ? 'Searching...' : 'Search'}
        </button>
      </form>

      {searchResults.length ? (
        <div className="public-experience-search-results">
          <div className="search-results-toolbar">
            <div className="search-results-summary">
              <strong>{searchResultsSummary}</strong>
            </div>
            {renderSearchPagination('top')}
          </div>
          <ul className="list">
            {searchResults.map((result) => {
              const semanticTier = semanticFitTier(result.semantic_score)
              const linkedVideo = sortedVideos.find((video) => video.id === result.video_id)
              const linkedObject = linkedVideo?.object_id
                ? filteredPublishedObjectsForPreview.find((objectRow) => objectRow.id === linkedVideo.object_id) || null
                : null
              const previewAvailable = Boolean(linkedVideo?.object_id && publishedObjectIds.has(linkedVideo.object_id))
              const resultActionEnabled = isPublicApp ? Boolean(result.evidence_url) : previewAvailable
              const humanizedVideoTitle = humanizeMediaTitle(result.video_title || linkedVideo?.title || '')
              const resultTitle = result.object_name
                || linkedObject?.name
                || (linkedVideo?.object_id === publicPreviewData?.object?.id ? publicPreviewData?.object?.name : '')
                || humanizedVideoTitle
                || 'Search result'
              const resultContext = [
                result.project_name,
                humanizedVideoTitle && humanizedVideoTitle !== resultTitle ? humanizedVideoTitle : ''
              ].filter(Boolean).join(' · ')

              return (
                <li key={result.segment_id} className="search-result-row">
                  <div className="list-main video-list-main">
                    <div className="search-result-heading">
                      <strong>{resultTitle} · {formatClock(result.start_ms)}</strong>
                      <div className="search-result-meta">
                        {result.lexical_match ? <span className="search-fit-chip lexical">Keyword match</span> : null}
                        {result.semantic_score != null && semanticTier ? (
                          <span
                            className={`search-fit-chip semantic ${semanticTier.tone}`}
                            title="Approximate semantic relevance score from vector similarity. Higher is closer, but it is not a probability."
                          >
                            {semanticTier.label} {formatSemanticFit(result.semantic_score)}
                          </span>
                        ) : null}
                      </div>
                    </div>
                    {resultContext ? <p className="muted">{resultContext}</p> : null}
                    {Number.isFinite(Number(result.context_start_ms)) && Number.isFinite(Number(result.context_end_ms)) ? (
                      <p className="muted search-result-context-window">
                        Play moment {formatClipClock(result.context_start_ms)}-{formatClipClock(result.context_end_ms)}
                      </p>
                    ) : null}
                    <p className="search-result-text">{result.text}</p>
                  </div>
                  <div className="topbar-actions search-result-actions">
                    <button
                      type="button"
                      className="search-result-button"
                      disabled={!resultActionEnabled}
                      onClick={() => handleSearchResultSelect(result)}
                    >
                      {isPublicApp ? 'Open evidence' : 'Play moment'}
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
          {renderSearchPagination('bottom')}
          <div className="search-results-clear-row">
            <button type="button" className="ghost search-clear-button" onClick={clearActiveSearchResults}>
              Clear results
            </button>
          </div>
        </div>
      ) : (
        <ActionEmptyState
          title={searchForm.query.trim() ? 'No matching moments' : 'Search published moments'}
          text={searchForm.query.trim()
            ? 'Try another lexical or semantic query to find related moments.'
            : 'Run a lexical or semantic query to preview object-linked evidence.'}
          actionLabel={searchForm.query.trim() ? 'Search again' : 'Start search'}
          onAction={focusSearchField}
        />
      )}
    </div>
  )

  const evidenceHeading = (
    <div className="public-experience-section-heading">
      <h3>Evidence</h3>
      <p className="muted">Hear community members interpret the object in their own words and connect those perspectives to specific details on the model.</p>
    </div>
  )

  const evidenceVideoPanel = (
    <div className="viewer-video public-experience-video-panel" ref={publicPreviewViewerRef}>
      <div className="public-experience-video-stage">
        {publicPreviewData?.video ? (
          publicPreviewData.videoUrl ? (
            <video
              className="public-experience-video-element"
              key={`${publicPreviewData.object?.id || 'object'}:${publicPreviewData.video?.id || 'video'}`}
              ref={publicPreviewVideoRef}
              src={publicPreviewData.videoUrl}
              controls={!isMobileShell}
              controlsList={isMobileShell ? 'nofullscreen nodownload noremoteplayback' : undefined}
              playsInline
              disablePictureInPicture
              disableRemotePlayback
              preload="metadata"
              onLoadedMetadata={(event) => {
                const durationMs = Number.isFinite(event.currentTarget.duration)
                  ? Math.floor(event.currentTarget.duration * 1000)
                  : 0
                event.currentTarget.setAttribute('playsinline', 'true')
                event.currentTarget.setAttribute('webkit-playsinline', 'true')
                setMobileVideoDurationMs(durationMs)
                setMobileVideoIsPlaying(!event.currentTarget.paused)
                setMobileClipTransitionBusy(false)
                applyPublicPreviewPendingPlayback(event.currentTarget)
              }}
              onTimeUpdate={(event) => {
                const media = event.currentTarget
                const currentMs = Math.floor((media.currentTime || 0) * 1000)
                if (Number.isFinite(media.duration)) {
                  setMobileVideoDurationMs(Math.floor(media.duration * 1000))
                }
                const clipEndMs = (
                  publicPreviewClipPlayback?.videoId === publicPreviewData.video?.id
                    ? Math.max(0, Number(publicPreviewClipPlayback?.endMs || 0))
                    : 0
                )
                setPublicPreviewCurrentMs(currentMs)
              }}
              onCanPlay={() => setMobileClipTransitionBusy(false)}
              onPlay={() => setMobileVideoIsPlaying(true)}
              onPlaying={() => setMobileClipTransitionBusy(false)}
              onPause={() => setMobileVideoIsPlaying(false)}
              onSeeked={() => setMobileClipTransitionBusy(false)}
              onWaiting={() => {
                if (isMobileShell && publicPreviewSelectedAnnotation && publicPreviewSelectedAnnotationPlaylist.length > 1) {
                  setMobileClipTransitionBusy(true)
                }
              }}
              onEnded={() => {
                setMobileVideoIsPlaying(false)
                finishPublicPreviewClip()
              }}
            />
          ) : (
            <div className="public-experience-video-empty">
              <ActionEmptyState
                title={publicPreviewData.video.status === 'READY' ? 'Playback unavailable' : 'Video processing'}
                text={
                  publicPreviewData.video.status === 'READY'
                    ? 'The selected video is linked, but the playback asset could not be loaded.'
                    : `The selected video is still processing. ${publicSurfaceTitle} only exposes approved playback once the asset is ready.`
                }
                actionLabel={canChooseAnotherVideo ? 'Choose another video' : primaryObjectActionLabel}
                onAction={canChooseAnotherVideo ? focusVideoSelector : primaryObjectAction}
              />
            </div>
          )
        ) : (
          <div className="public-experience-video-empty">
            <ActionEmptyState
              title="No published video"
              text={isPublicApp
                ? 'This object does not currently include a published evidence video.'
                : 'Add or select a published preview video to review playback here.'}
              actionLabel={primaryObjectActionLabel}
              onAction={primaryObjectAction}
            />
          </div>
        )}
      </div>

      {!isMobileShell ? evidencePlaylistBar : null}
    </div>
  )

  const evidenceTranscriptPanel = (
    <div className="viewer-transcript public-experience-transcript-panel">
      <div className="public-experience-transcript-header">
        <h3>Transcript</h3>
      </div>
      <div className="transcript-list public-experience-transcript-list" ref={publicPreviewTranscriptListRef}>
        {publicPreviewSegments.length ? (
          publicPreviewSegments.map((segment, index) => (
            <button
              type="button"
              key={segment.id}
              data-public-preview-segment-index={index}
              className={`transcript-line ${index === publicPreviewCurrentSegmentIndex ? 'active' : ''}`}
              disabled={!publicPreviewData?.videoUrl}
              onClick={() => jumpToPublicPreviewMs(Number(segment.start_ms || 0), true)}
            >
              <span>{formatClock(segment.start_ms)}</span>
              <strong>{segment.text}</strong>
            </button>
          ))
        ) : (
          <ActionEmptyState
            title="No transcript context"
            text="This video does not include transcript context."
            actionLabel={canChooseAnotherVideo ? 'Choose another video' : primaryObjectActionLabel}
            onAction={canChooseAnotherVideo ? focusVideoSelector : primaryObjectAction}
          />
        )}
      </div>
    </div>
  )

  const evidenceStack = (
    <div className="public-experience-evidence-stack">
      {evidenceHeading}
      {evidenceVideoPanel}
      {evidenceTranscriptPanel}
    </div>
  )

  const mobileSequenceCount = publicPreviewSelectedAnnotationPlaylist.length
  const showMobileClipTransition = isMobileShell
    && mobileEvidenceOpen
    && Boolean(publicPreviewSelectedAnnotation)
    && mobileSequenceCount > 1
    && Boolean(mobileClipTransitionBusy || mobileClipTransitionCue)
  const mobileClipTransitionLabel = mobileClipTransitionBusy
    ? `Loading clip ${publicPreviewClipIndex + 1} of ${mobileSequenceCount}`
    : mobileClipTransitionCue
  const mobileClipTransitionNote = mobileClipTransitionBusy
    ? 'Preparing the next authored clip.'
    : 'Sequence handoff is ready.'

  const mobileEvidenceStack = (
    <div className="public-experience-mobile-video-only">
      {evidenceHeading}
      {evidenceVideoPanel}
      {evidencePlaylistBar}
      {showMobileClipTransition ? (
        <div className={`public-experience-mobile-clip-handoff ${mobileClipTransitionBusy ? 'busy' : 'ready'}`.trim()}>
          <div className="public-experience-mobile-clip-handoff-copy">
            <strong>{mobileClipTransitionLabel}</strong>
            <span>{mobileClipTransitionNote}</span>
          </div>
          <span className="public-experience-mobile-clip-handoff-bar" aria-hidden="true" />
        </div>
      ) : null}
      {mobileInlineControls}
    </div>
  )

  const modelCard = (
    <div className="public-experience-model-card">
      <div className="public-experience-model-card-header">
        <span className="public-experience-eyebrow">Spatial object view</span>
        <p className="muted">Select labels directly on the model to surface community interpretations connected to each detail.</p>
      </div>
      {publicPreviewData?.model ? (
        deferSearchMomentModel ? (
          <div className="model-canvas-empty public-experience-model-deferred" aria-hidden="true">
            <div className="model-canvas-empty-copy">
              <strong>3D view paused</strong>
              <p>Close the evidence sheet to resume the object model on mobile.</p>
            </div>
          </div>
        ) : (
          <ModelCanvas
            key={`${publicPreviewData.object?.id || 'object'}:${publicPreviewData.model?.id || 'model'}`}
            modelUrl={
              isPublicApp
                ? publicPreviewData.modelUrl
                : getObjectModelFileUrl(token, publicPreviewData.model.object_id, `${publicPreviewData.model.revision_number}-${publicPreviewData.model.updated_at}`)
            }
            modelTransform={publicPreviewModelTransform}
            defaultCameraView={publicPreviewCameraView}
            annotations={publicPreviewData.annotations || []}
            selectedAnnotationId={publicPreviewSelectedAnnotation?.id || ''}
            placementMode={false}
            cameraViewKey={`${publicPreviewData.model.id}:${publicPreviewData.model.updated_at}:${publicPreviewSelectedAnnotation?.id || 'default'}:public`}
            canvasDpr={isMobileShell ? 1 : [1, 1.5]}
            preferLowPower={isMobileShell}
            showAnnotationLabels={!isMobileShell || !mobileEvidenceOpen}
            isLoading={publicPreviewLoading}
            loadingEyebrow={isPublicApp ? 'Live object package' : 'Public preview package'}
            loadingTitle={publicPreviewData ? 'Updating 3D view...' : 'Preparing 3D view...'}
            loadingMessage={
              publicPreviewData
                ? 'Keeping the current stage steady while the next published model and annotations load.'
                : 'Loading the published model, annotations, and saved camera view.'
            }
            emptyTitle="No 3D model"
            emptyMessage={
              isPublicApp
                ? 'This object does not currently include a published 3D model.'
                : 'Add and publish a 3D model to review spatial evidence here.'
            }
            onSurfacePick={null}
            onTransformChange={null}
            onCameraViewChange={null}
            onSelectAnnotation={handleAnnotationSelect}
          />
        )
      ) : (
        <ActionEmptyState
          title="No 3D model"
          text={isPublicApp
            ? 'This object does not currently include a published 3D model.'
            : 'Add and publish a 3D model to review spatial evidence here.'}
          actionLabel={primaryObjectActionLabel}
          onAction={primaryObjectAction}
        />
      )}
    </div>
  )

  const momentCard = (
    <div className="public-experience-moment-card">
      <div className="public-experience-moment-heading">
        <h3>Current moment</h3>
        <p className="muted">The selected moment, playback, transcript, and model orientation stay synchronized.</p>
      </div>
      {(publicPreviewSelectedAnnotation || activeSearchMoment) ? (
        <div className="public-experience-moment-body">
          <div className="public-experience-moment-copy">
            <strong className="public-experience-moment-title">{currentMomentTitle}</strong>
            <div className="public-experience-moment-meta" aria-label="Current moment details">
              <span>{currentMomentVideoLabel}</span>
              <span>{formatClipClock(activeMomentRangeStartMs)}-{formatClipClock(activeMomentRangeEndMs)}</span>
            </div>
            <p className="public-experience-moment-summary">{currentMomentSummary}</p>
            {showMatchedExcerptNote ? (
              <span className="muted public-experience-context-note">
                Matched excerpt {formatClipClock(activeMatchedRangeStartMs)}-{formatClipClock(activeMatchedRangeEndMs)}
              </span>
            ) : null}
          </div>
          <div className="public-experience-moment-actions">
            <button type="button" className="ghost public-experience-moment-action" onClick={activateMomentPage}>
              {currentMomentActionLabel}
            </button>
          </div>
        </div>
      ) : (
        <ActionEmptyState
          title="No moment selected"
          text="Choose a label on the model to load its linked evidence moment."
          actionLabel="Search moments"
          onAction={focusSearchField}
        />
      )}
    </div>
  )

  const mobileMomentStrip = (
    <div className="public-experience-mobile-moment-strip">
      <div className="public-experience-mobile-moment-copy">
        <span className="public-experience-eyebrow">Current moment</span>
        {(publicPreviewSelectedAnnotation || activeSearchMoment) ? (
          <>
            <strong>{publicPreviewSelectedAnnotation?.title || activeSearchMoment?.title || 'Search result'}</strong>
            <span className="muted">
              {publicPreviewSelectedAnnotation
                ? (publicPreviewClipVideo?.title || publicPreviewData?.video?.title || 'Linked video')
                : (activeSearchMoment?.videoTitle || publicPreviewData?.video?.title || 'Linked video')}
              {' · '}
              {formatClipClock(activeMomentRangeStartMs)}-{formatClipClock(activeMomentRangeEndMs)}
            </span>
          </>
        ) : (
          <p className="muted">Select a label on the model to open the linked interpretation and transcript moment.</p>
        )}
      </div>
      <button
        type="button"
        className="ghost public-experience-mobile-moment-action"
        onClick={activateMomentPage}
      >
        {currentMomentActionLabel}
      </button>
    </div>
  )

  const mobileEvidenceTitle = publicPreviewSelectedAnnotation?.title
    || activeSearchMoment?.title
    || publicPreviewClipVideo?.title
    || publicPreviewData?.video?.title
    || 'Evidence'

  const mobileOverlayEyebrow = publicPreviewSelectedAnnotation || activeSearchMoment ? 'Play moment' : 'Evidence'

  const mobileEvidenceMeta = publicPreviewSelectedAnnotation || activeSearchMoment
    ? `${formatClipClock(activeMomentRangeStartMs)}-${formatClipClock(activeMomentRangeEndMs)}`
    : (publicPreviewData?.video ? 'Object-linked evidence and synchronized transcript' : 'Choose a moment or another object to load evidence.')

  const mobileEvidenceSubmeta = !publicPreviewSelectedAnnotation && activeSearchMoment && activeSearchMoment.contextStartMs !== activeSearchMoment.matchedStartMs
    ? `Matched excerpt ${formatClipClock(activeMatchedRangeStartMs)}-${formatClipClock(activeMatchedRangeEndMs)}`
    : ''

  return (
    <>
      <a
        className="skip-link"
        href={`#${PUBLIC_PREVIEW_MAIN_CONTENT_ID}`}
        onClick={() => focusSkipTarget(PUBLIC_PREVIEW_MAIN_CONTENT_ID)}
      >
        Skip to content
      </a>
      <section
        id={PUBLIC_PREVIEW_MAIN_CONTENT_ID}
        ref={shellRef}
        tabIndex={-1}
        role="main"
        className={`public-experience-shell skip-link-target motion-shell-enter ${isMobileShell ? 'mobile-shell' : ''}`.trim()}
        data-mobile-overflow-state="idle"
      >
      <div className="public-experience-package-bar">
        <div className="field">
          <label className="ingest-label" htmlFor="public-preview-project-select">Project</label>
          <select
            id="public-preview-project-select"
            value={modelProjectId}
            onChange={(event) => {
              if (isMobileShell) {
                closeMobileEvidenceOverlay({ normalizeViewport: false })
              }
              setPublicPreviewSearchMoment(null)
              setPublicPreviewVideoId('')
              setPublicPreviewClipPlayback(null)
              setPublicPreviewPendingPlayback(null)
              setPublicPreviewCurrentMs(0)
              setModelProjectId(event.target.value)
              setModelObjectId('')
            }}
          >
            <option value="">Select project</option>
            {publishedProjects.map((project) => (
              <option value={project.id} key={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="ingest-label" htmlFor="public-preview-object-select">Object package</label>
          <select
            id="public-preview-object-select"
            value={modelObjectId}
            onChange={(event) => {
              const nextObjectId = event.target.value
              const nextObject = filteredPublishedObjectsForPreview.find((objectRow) => objectRow.id === nextObjectId) || null
              if (isMobileShell) {
                closeMobileEvidenceOverlay({ normalizeViewport: false })
              }
              setPublicPreviewSearchMoment(null)
              setPublicPreviewVideoId('')
              setPublicPreviewClipPlayback(null)
              setPublicPreviewPendingPlayback(null)
              setPublicPreviewCurrentMs(0)
              if (nextObject?.project_id && nextObject.project_id !== modelProjectId) {
                setModelProjectId(nextObject.project_id)
              }
              setModelObjectId(nextObjectId)
            }}
          >
            <option value="">Select object</option>
            {filteredPublishedObjectsForPreview.map((objectRow) => {
              const openNow = openNowObjectIds ? openNowObjectIds.has(objectRow.id) : true
              const suffix = isPublicApp ? '' : openNow ? ' · Open now' : ' · Published · incomplete'
              return (
                <option value={objectRow.id} key={objectRow.id}>
                  {objectRow.name}{suffix}
                </option>
              )
            })}
          </select>
        </div>

        {publicPreviewData?.videos?.length > 1 ? (
          <div className="field">
            <label className="ingest-label" htmlFor="public-preview-video-select">Evidence video</label>
            <select
              id="public-preview-video-select"
              value={publicPreviewVideoId || publicPreviewData.video?.id || ''}
              onChange={(event) => {
                setPublicPreviewSearchMoment(null)
                setPublicPreviewVideoId(event.target.value)
                setPublicPreviewClipPlayback(null)
                setPublicPreviewPendingPlayback(null)
                setPublicPreviewCurrentMs(0)
              }}
            >
              {publicPreviewData.videos.map((video) => (
                <option value={video.id} key={video.id}>
                  {video.title} · {video.status}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>

      {loadingBanner}

      {publicPreviewData ? (
        <>
          <header className="public-experience-header">
            <div className="public-experience-header-main">
              <div className="public-experience-heading">
                <p className="kicker">{isPublicApp ? 'Published Object Package' : 'Public Preview Package'}</p>
                <h2>{publicPreviewData.object?.name || 'Untitled object'}</h2>
                {!isPublicApp && publicPreviewData.object?.id && openNowObjectIds ? (
                  openNowObjectIds.has(publicPreviewData.object.id) ? (
                    <p className="publication-open-now-pill open-now">
                      <span className="publication-open-now-dot" aria-hidden="true" />
                      Open now
                    </p>
                  ) : (
                    <p className="publication-open-now-pill incomplete">
                      <span className="publication-open-now-dot" aria-hidden="true" />
                      Published · incomplete
                    </p>
                  )
                ) : null}
                {activeProject ? (
                  <p className="public-experience-project-line">
                    Project: <span>{activeProject.name}</span>
                  </p>
                ) : null}
              </div>

              <div className="public-experience-data-block">
                <div className="public-experience-data-heading">
                  <span className="public-experience-data-label">Object Data</span>
                  {isMobileShell ? <span className="public-experience-mobile-summary">{evidenceSummary}</span> : null}
                </div>
                <p className="public-experience-standfirst">{objectSummary}</p>
              </div>
            </div>

            <div className="public-experience-actions">
              <button type="button" onClick={activateEvidenceView}>
                {hasEvidencePage ? 'Open evidence page' : 'Open evidence'}
              </button>
              {!isMobileShell ? (
                <button type="button" className="ghost" onClick={activateSearchView}>
                  Search evidence
                </button>
              ) : null}
              {publicPreviewData.object?.is_published && publicPreviewData.object?.external_url ? (
                <a
                  className="button-link secondary public-preview-collection-link"
                  href={publicPreviewData.object.external_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Collection Website
                </a>
              ) : null}
            </div>
          </header>

          {isMobileShell ? (
            <>
              <div className="public-experience-mobile-mode-bar" role="tablist" aria-label="Mobile public view">
                <button
                  type="button"
                  className={`public-experience-mobile-nav-button ${mobilePrimaryMode === 'object' ? 'active' : ''}`.trim()}
                  onClick={activateObjectView}
                >
                  Object
                </button>
                <button
                  type="button"
                  className={`public-experience-mobile-nav-button ${mobilePrimaryMode === 'search' ? 'active' : ''}`.trim()}
                  onClick={activateSearchView}
                >
                  Search
                </button>
              </div>

              <div className="public-experience-mobile-layout">
                {mobilePrimaryMode === 'object' ? (
                  <section className="public-experience-mobile-object-stack">
                    {modelCard}
                    {mobileMomentStrip}
                  </section>
                ) : null}

                {mobilePrimaryMode === 'search' ? (
                  <section className="public-experience-drawer public-experience-mobile-surface public-experience-mobile-search-surface open">
                    {searchStack}
                  </section>
                ) : null}
              </div>

              {mobileEvidenceOpen ? (
                <div
                  className="public-experience-mobile-overlay-backdrop"
                  onClick={closeMobileEvidenceOverlay}
                  role="presentation"
                >
                  <section
                    className="public-experience-mobile-evidence-overlay"
                    onClick={(event) => event.stopPropagation()}
                    role="dialog"
                    aria-modal="true"
                    aria-label="Evidence overlay"
                  >
                    <div className="public-experience-mobile-overlay-header">
                      <div className="public-experience-mobile-overlay-copy">
                        <span className="public-experience-eyebrow">{mobileOverlayEyebrow}</span>
                        <h3>{mobileEvidenceTitle}</h3>
                        <p>{mobileEvidenceMeta}</p>
                        {mobileEvidenceSubmeta ? <span className="public-experience-mobile-overlay-submeta">{mobileEvidenceSubmeta}</span> : null}
                      </div>
                      <button
                        type="button"
                        className="ghost public-experience-mobile-overlay-close"
                        onClick={closeMobileEvidenceOverlay}
                        aria-label="Close evidence"
                      >
                        Close
                      </button>
                    </div>
                    <div className="public-experience-mobile-overlay-body">
                      {mobileEvidenceStack}
                    </div>
                  </section>
                </div>
              ) : null}
            </>
          ) : (
            <div className={`public-experience-main ${drawerOpen ? '' : 'drawer-collapsed'}`.trim()}>
              <section className="public-experience-model-column">
                {modelCard}
                {momentCard}
              </section>

              <aside className={`public-experience-drawer ${drawerOpen ? 'open' : 'collapsed'}`.trim()}>
                {drawerOpen ? (
                  <>
                    <div className="public-experience-drawer-header">
                      <div className="public-experience-drawer-title">
                        <span className="public-experience-eyebrow">Evidence panel</span>
                        <h3>{drawerView === 'search' ? 'Search' : 'Evidence'}</h3>
                      </div>
                      <div className="public-experience-drawer-header-actions">
                        <div className="public-experience-drawer-tabs">
                          <DrawerToggle active={drawerView === 'evidence'} onClick={() => setDrawerView('evidence')}>Evidence</DrawerToggle>
                          <DrawerToggle active={drawerView === 'search'} onClick={() => setDrawerView('search')}>Search</DrawerToggle>
                        </div>
                        <button
                          type="button"
                          className="ghost public-experience-drawer-control"
                          aria-expanded="true"
                          onClick={() => setDrawerOpen(false)}
                        >
                          Collapse
                        </button>
                      </div>
                    </div>

                    <div className="public-experience-drawer-body">
                      {drawerView === 'search' ? searchStack : evidenceStack}
                    </div>
                  </>
                ) : (
                  <div className="public-experience-drawer-collapsed-rail">
                    <button
                      type="button"
                      className={`ghost public-experience-rail-button ${drawerView === 'evidence' ? 'active' : ''}`.trim()}
                      onClick={() => {
                        setDrawerView('evidence')
                        setDrawerOpen(true)
                      }}
                    >
                      Evidence
                    </button>
                    <button
                      type="button"
                      className={`ghost public-experience-rail-button ${drawerView === 'search' ? 'active' : ''}`.trim()}
                      onClick={() => {
                        setDrawerView('search')
                        setDrawerOpen(true)
                      }}
                    >
                      Search
                    </button>
                    <button
                      type="button"
                      className="ghost public-experience-drawer-control"
                      aria-expanded="false"
                      onClick={() => setDrawerOpen(true)}
                    >
                      Open
                    </button>
                  </div>
                )}
              </aside>
            </div>
          )}
        </>
      ) : publicPreviewLoading ? (
        loadingShell
      ) : (
        <ActionEmptyState
          title="No object selected"
          text={isPublicApp ? 'Choose an object to load published evidence.' : 'Choose an object to load its published preview package.'}
          actionLabel="Choose an object"
          onAction={focusObjectSelector}
        />
      )}
      </section>
    </>
  )
}
