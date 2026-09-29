import { useCallback, useEffect, useRef, useState } from 'react'

import MomentRail from './MomentRail'
import { calculateTranscriptScrollTop } from './transcriptScroll'
import { PUBLIC_BROWSE_PATH } from '../../lib/navigation'
import { applyDocumentThemeChrome } from '../../lib/documentTheme'

// Studio S1 — flag-gated shell. Mounts the EXISTING ModelCanvas + video element
// in a shared 4:3 / 16:9 stage, plus the shared-clock spine: a linked transport
// (scrubber) and a transcript "score" beneath the stage. The video element is
// the single source of truth for the clock; this layer only seeks it via the
// `onSeek` shape and reflects `currentMs`. No moment rail, auto-focus, or search
// redesign here (those are later phases). Legacy evidence page is unchanged and
// renders by default; this renders only when `?studio=1` is present.

const THEME_STORAGE_KEY = 'loci.studio.theme'
const THEME_MODES = ['system', 'light', 'muted-light', 'dark']

// View modes: Console = the full workspace (search + rail + transcript);
// Focus = a decluttered, stage-forward reading view. Console is the default so
// the fuller experience is what users land in.
const VIEW_STORAGE_KEY = 'loci.studio.view'
const VIEW_MODES = ['console', 'focus']

function readStoredView() {
  if (typeof window === 'undefined') {
    return 'console'
  }
  try {
    const stored = window.localStorage.getItem(VIEW_STORAGE_KEY)
    return VIEW_MODES.includes(stored) ? stored : 'console'
  } catch {
    return 'console'
  }
}

function useStudioView() {
  const [view, setView] = useState(readStoredView)
  const selectView = useCallback((nextView) => {
    if (!VIEW_MODES.includes(nextView)) {
      return
    }
    setView(nextView)
    try {
      window.localStorage.setItem(VIEW_STORAGE_KEY, nextView)
    } catch {
      // Best-effort; the in-memory view still applies.
    }
  }, [])
  return { view, selectView }
}

function ViewSegmentedControl({ view, onSelect }) {
  return (
    <div className="studio-view-control" role="radiogroup" aria-label="Studio view">
      {VIEW_MODES.map((viewMode) => {
        const isActive = viewMode === view
        return (
          <button
            key={viewMode}
            type="button"
            role="radio"
            aria-checked={isActive}
            tabIndex={isActive ? 0 : -1}
            className={`studio-view-option${isActive ? ' is-active' : ''}`}
            onClick={() => onSelect(viewMode)}
          >
            {viewMode === 'console' ? 'Console' : 'Focus'}
          </button>
        )
      })}
    </div>
  )
}

function readStoredMode() {
  if (typeof window === 'undefined') {
    return 'system'
  }
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return THEME_MODES.includes(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

function systemPrefersDark() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

// `light` → Cobalt, `dark` → Darkroom, `system` → follows the OS preference.
function resolvePalette(mode, prefersDark) {
  if (mode === 'light') {
    return 'cobalt'
  }
  if (mode === 'muted-light') {
    return 'muted-light'
  }
  if (mode === 'dark') {
    return 'darkroom'
  }
  return prefersDark ? 'darkroom' : 'cobalt'
}

function useStudioTheme() {
  const [mode, setMode] = useState(readStoredMode)
  const [prefersDark, setPrefersDark] = useState(systemPrefersDark)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined
    }

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = (event) => setPrefersDark(event.matches)

    setPrefersDark(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    }

    mediaQuery.addListener(handleChange)
    return () => mediaQuery.removeListener(handleChange)
  }, [])

  const selectMode = useCallback((nextMode) => {
    if (!THEME_MODES.includes(nextMode)) {
      return
    }
    setMode(nextMode)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextMode)
    } catch {
      // Persistence is best-effort; the in-memory mode still applies.
    }
  }, [])

  const palette = resolvePalette(mode, prefersDark)

  // Keep mobile browser chrome + safe-area fill in sync with the object-page
  // Studio palette (P4.8-C #4).
  useEffect(() => {
    applyDocumentThemeChrome(palette)
  }, [palette])

  return {
    mode,
    palette,
    selectMode,
  }
}

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return false
    }
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  })

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined
    }
    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    const handleChange = (event) => setReduced(event.matches)
    setReduced(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    }
    mediaQuery.addListener(handleChange)
    return () => mediaQuery.removeListener(handleChange)
  }, [])

  return reduced
}

// Fallback clock formatter (mm:ss / h:mm:ss) used only if the host does not pass
// its own `formatClock`. Kept identical in shape to the evidence-page formatter.
function defaultFormatClock(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const mm = String(minutes).padStart(2, '0')
  const ss = String(seconds).padStart(2, '0')
  return hours > 0 ? `${String(hours).padStart(2, '0')}:${mm}:${ss}` : `${mm}:${ss}`
}

// Linked transport — a real timeline. A native range gives full slider
// semantics + keyboard support; moment pips are overlaid on the track and, when
// clicked, run the same seek/fan-out path as the moment rail (via onSelectMarker).
function StudioTransport({ currentMs, durationMs, onSeek, formatClock, markers = [], onSelectMarker }) {
  const hasDuration = Number.isFinite(durationMs) && durationMs > 0
  const max = hasDuration ? durationMs : 1
  const value = Math.min(Math.max(0, Number(currentMs) || 0), max)
  const currentLabel = formatClock(value)
  const durationLabel = hasDuration ? formatClock(max) : '--:--'
  const activeMarkerMs = value
  const pips = hasDuration
    ? markers.filter((m) => Number.isFinite(Number(m.startMs)) && Number(m.startMs) <= max)
    : []

  return (
    <section className="studio-transport" aria-label="Playback timeline">
      <div className="studio-transport-track">
        <input
          type="range"
          className="studio-transport-scrubber"
          min={0}
          max={max}
          step={250}
          value={value}
          disabled={!hasDuration}
          aria-label="Seek video"
          aria-valuetext={`${currentLabel} of ${durationLabel}`}
          onChange={(event) => onSeek({ seekMs: Number(event.currentTarget.value), source: 'transport' })}
        />
        {pips.length ? (
          <div className="studio-transport-pips" aria-hidden={false}>
            {pips.map((marker) => {
              const pct = Math.min(100, Math.max(0, (Number(marker.startMs) / max) * 100))
              const isActive = Math.abs(Number(marker.startMs) - activeMarkerMs) < 750
              return (
                <button
                  key={marker.id}
                  type="button"
                  className={`studio-transport-pip${isActive ? ' is-active' : ''}`}
                  style={{ left: `${pct}%` }}
                  aria-label={`Jump to ${marker.label || 'moment'} at ${formatClock(marker.startMs)}`}
                  onClick={() => onSelectMarker?.(marker)}
                />
              )
            })}
          </div>
        ) : null}
      </div>
      <div className="studio-transport-times">
        <span className="studio-transport-time studio-transport-time-current">{currentLabel}</span>
        <span className="studio-transport-time studio-transport-time-duration">{durationLabel}</span>
      </div>
    </section>
  )
}

// Accessible object-description disclosure shown directly under the Studio
// title. Uses native <details>/<summary> so keyboard + screen-reader behavior
// is correct. Body renders the public payload's object summary as text and,
// when present, a safe external "Open collection record" link.
function isSafeExternalUrl(url) {
  if (typeof url !== 'string') {
    return false
  }
  return /^https?:\/\//i.test(url.trim())
}

function ObjectDescriptionDisclosure({ summary, externalUrl }) {
  const hasSummary = typeof summary === 'string' && summary.trim().length > 0
  const hasLink = isSafeExternalUrl(externalUrl)
  if (!hasSummary && !hasLink) {
    return null
  }
  return (
    <details className="studio-object-disclosure">
      <summary className="studio-object-disclosure-summary">
        <span>About this object</span>
        <span className="studio-object-disclosure-chevron" aria-hidden="true" />
      </summary>
      <div className="studio-object-disclosure-body">
        {hasSummary ? <p className="studio-object-disclosure-text">{summary}</p> : null}
        {hasLink ? (
          <a
            className="studio-object-disclosure-link"
            href={externalUrl}
            target="_blank"
            rel="noreferrer"
          >
            Open collection record
          </a>
        ) : null}
      </div>
    </details>
  )
}

// Transcript "score" — reads beneath the stage. The active line follows
// `activeSegmentIndex` (computed by the host from the shared clock) and clicking
// a line seeks the video. Lines are buttons, so keyboard/Enter works natively.
function StudioTranscript({ segments, activeSegmentIndex, onSeek, formatClock, reducedMotion, viewMode, autoScrollOnLoad = false }) {
  const listRef = useRef(null)
  const rowRefs = useRef(new Map())
  const initialScrollHandledRef = useRef(false)

  useEffect(() => {
    initialScrollHandledRef.current = false
  }, [autoScrollOnLoad, segments])

  useEffect(() => {
    if (activeSegmentIndex < 0) {
      return
    }
    // P2: on a plain object load, don't scroll the transcript on the first
    // settle (land at top); deep links still jump. Follow-along resumes after.
    if (!initialScrollHandledRef.current) {
      initialScrollHandledRef.current = true
      if (!autoScrollOnLoad) {
        return
      }
    }
    const list = listRef.current
    const row = rowRefs.current.get(activeSegmentIndex)
    if (!list || !(row instanceof HTMLElement)) {
      return
    }
    // row.offsetTop is relative to its offsetParent, which differs between the
    // Console and Focus layouts. Use viewport geometry to measure the line from
    // this scroll list so follow-along stays inside the list in both views.
    const listRect = list.getBoundingClientRect()
    const rowRect = row.getBoundingClientRect()
    const target = calculateTranscriptScrollTop({
      listTop: listRect.top + list.clientTop,
      rowTop: rowRect.top,
      scrollTop: list.scrollTop,
      listHeight: list.clientHeight,
      rowHeight: rowRect.height,
      scrollHeight: list.scrollHeight,
    })
    list.scrollTo({ top: target, behavior: reducedMotion ? 'auto' : 'smooth' })
  }, [activeSegmentIndex, reducedMotion, autoScrollOnLoad, viewMode])

  return (
    <section className="studio-transcript" aria-label="Transcript">
      <h2 className="studio-transcript-heading">Transcript</h2>
      {segments.length ? (
        <div className="studio-transcript-list" ref={listRef}>
          {segments.map((segment, index) => {
            const isActive = index === activeSegmentIndex
            return (
              <button
                key={`${segment.position ?? index}:${segment.start_ms}:${segment.end_ms}`}
                ref={(node) => {
                  if (node) {
                    rowRefs.current.set(index, node)
                    return
                  }
                  rowRefs.current.delete(index)
                }}
                type="button"
                className={`studio-transcript-line${isActive ? ' is-active' : ''}`}
                aria-current={isActive ? 'true' : undefined}
                onClick={() => onSeek({ seekMs: Number(segment.start_ms || 0), source: 'transcript' })}
              >
                <time className="studio-transcript-time">{formatClock(segment.start_ms)}</time>
                <span className="studio-transcript-text">{segment.text}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <p className="studio-transcript-empty muted">This session does not include a published transcript.</p>
      )}
    </section>
  )
}

// P4.5 — minimal light/dark icon toggle. Replaces the System/Light/Dark segmented
// control while PRESERVING system-preference support: `mode` starts at 'system'
// (follows the OS, and keeps following it until the user clicks), and each click
// sets an explicit palette opposite the one currently shown. The icon reflects the
// action (sun = switch to light, moon = switch to dark).
function ThemeToggleButton({ palette, onSelect }) {
  const isDark = palette === 'darkroom'
  const next = isDark ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="studio-theme-toggle"
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-pressed={isDark}
      onClick={() => onSelect(next)}
    >
      {isDark ? (
        // Sun — switch to light
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="4.2" fill="currentColor" />
          <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <line x1="12" y1="2.5" x2="12" y2="5" />
            <line x1="12" y1="19" x2="12" y2="21.5" />
            <line x1="2.5" y1="12" x2="5" y2="12" />
            <line x1="19" y1="12" x2="21.5" y2="12" />
            <line x1="5.2" y1="5.2" x2="6.9" y2="6.9" />
            <line x1="17.1" y1="17.1" x2="18.8" y2="18.8" />
            <line x1="5.2" y1="18.8" x2="6.9" y2="17.1" />
            <line x1="17.1" y1="6.9" x2="18.8" y2="5.2" />
          </g>
        </svg>
      ) : (
        // Moon — switch to dark
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <path
            d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.8 6.8 0 0 0 9.8 9.8z"
            fill="currentColor"
          />
        </svg>
      )}
    </button>
  )
}

// Clip sequence for the currently selected moment — restores the published
// multi-clip "switch between clip windows" control (fix P4.1 #5). Renders even
// for a single clip (shows its window); a multi-clip moment gets a real switcher.
function StudioClipSequence({ clips, activeClipId, onSelect, formatClock }) {
  if (!clips.length) {
    return null
  }
  return (
    <section className="studio-clip-sequence" aria-label="Clips in this moment">
      <h2 className="studio-clip-sequence-heading">
        Clips in this moment <span className="studio-clip-sequence-count">{clips.length}</span>
      </h2>
      <div className="studio-clip-sequence-track" role="group" aria-label="Clip windows">
        {clips.map((clip) => {
          const isActive = clip.id === activeClipId
          return (
            <button
              key={clip.id}
              type="button"
              aria-pressed={isActive}
              className={`studio-clip-chip${isActive ? ' is-active' : ''}`}
              onClick={() => onSelect?.(clip.id)}
            >
              <span className="studio-clip-chip-label">{clip.label}</span>
              <time className="studio-clip-chip-window">
                {formatClock(clip.startMs)}–{formatClock(clip.endMs)}
              </time>
            </button>
          )
        })}
      </div>
    </section>
  )
}

// Slim, persistent contextual cue between the stage and the timeline. Never
// overlays a viewer; teaches the annotation→video interaction in context and,
// once a moment is playing, reflects it while keeping the discoverability line.
function StudioInteractionHint({ activeMoment, formatClock }) {
  const watching = activeMoment && activeMoment.title
    ? `Watching ${activeMoment.title}${Number.isFinite(activeMoment.startMs) ? ` · ${formatClock(activeMoment.startMs)}` : ''}`
    : ''
  return (
    <section className="studio-interaction-hint" aria-live="polite">
      {watching ? <span className="studio-interaction-hint-watching">{watching}</span> : null}
      <span className="studio-interaction-hint-cue">
        Click an annotation on the object to watch its video moment.
      </span>
    </section>
  )
}

export default function StudioShell({
  mainContentId,
  autoScrollOnLoad = false,
  objectTitle,
  objectSummary = '',
  objectExternalUrl = '',
  projectName = '',
  onReturnHome,
  citeSlot = null,
  videoTitle,
  modelSlot,
  videoSlot,
  activeMoment = null,
  currentMs = 0,
  durationMs = 0,
  onSeek,
  formatClock = defaultFormatClock,
  transcriptSegments = [],
  activeSegmentIndex = -1,
  railItems = [],
  momentMarkers = [],
  selectedRailItemId = '',
  onSelectRailItem,
  clipSequence = [],
  activeClipId = '',
  onSelectClip,
  searchSlot = null,
  overlaySlot = null,
}) {
  const { mode, palette, selectMode } = useStudioTheme()
  const { view, selectView } = useStudioView()
  const reducedMotion = usePrefersReducedMotion()
  const handleSeek = useCallback(
    (target) => {
      if (typeof onSeek === 'function') {
        onSeek(target)
      }
    },
    [onSeek],
  )

  return (
    <main
      id={mainContentId}
      tabIndex={-1}
      className="studio-shell skip-link-target"
      data-studio-theme={palette}
      data-studio-theme-mode={mode}
      data-studio-view={view}
    >
      <header className="studio-chrome">
        {/* P4.5: brand, title, and controls share ONE fixed row. The About
            disclosure lives BELOW this row so expanding it never pushes the
            controls down into the body (review #6). */}
        <div className="studio-chrome-row">
          <a
            className="studio-brand surface-branding-link"
            href={PUBLIC_BROWSE_PATH}
            aria-label="Go to the public homepage"
          >
            {/* Composed lockup so the mark keeps its axis colors while the wordmark
                flips to high-contrast white in the Darkroom palette (fix P4.1 #1). */}
            <img className="studio-brand-mark" src="/branding/mark.svg" alt="" aria-hidden="true" decoding="async" />
            <img className="studio-brand-wordmark" src="/branding/wordmark.svg" alt="Loci" decoding="async" />
          </a>
          {objectTitle ? (
            <div className="studio-chrome-title">
              <span className="public-experience-eyebrow">{projectName || 'Studio'}</span>
              <strong>{objectTitle}</strong>
            </div>
          ) : null}
          <div className="studio-chrome-controls">
            <a
              className="studio-return-home"
              href={PUBLIC_BROWSE_PATH}
              onClick={(event) => {
                if (typeof onReturnHome === 'function') {
                  event.preventDefault()
                  onReturnHome()
                }
              }}
            >
              Return to homepage
            </a>
            <ViewSegmentedControl view={view} onSelect={selectView} />
            <ThemeToggleButton palette={palette} onSelect={selectMode} />
          </div>
        </div>
        {objectTitle ? (
          <ObjectDescriptionDisclosure summary={objectSummary} externalUrl={objectExternalUrl} />
        ) : null}
      </header>

      {searchSlot ? (
        <div className="studio-search-region">{searchSlot}</div>
      ) : null}

      <section className="studio-stage" aria-label="Object and session stage">
        <div className="studio-object-panel" aria-label="3D object view">
          <div className="studio-panel-surface studio-object-surface">
            {modelSlot}
          </div>
        </div>
        <div className="studio-video-panel" aria-label={objectTitle ? `Session video for ${objectTitle}` : 'Session video'}>
          <div className="studio-panel-surface studio-video-surface">
            {videoSlot || (
              <div className="studio-video-empty">
                <strong>No published video</strong>
                <p className="muted">This object does not include published video playback.</p>
              </div>
            )}
          </div>
        </div>
      </section>

      <div className="studio-moment-bar">
        <StudioInteractionHint activeMoment={activeMoment} formatClock={formatClock} />
        {citeSlot ? <div className="studio-cite-slot">{citeSlot}</div> : null}
      </div>

      {videoSlot ? (
        <StudioTransport
          currentMs={currentMs}
          durationMs={durationMs}
          onSeek={handleSeek}
          formatClock={formatClock}
          markers={momentMarkers}
          onSelectMarker={onSelectRailItem}
        />
      ) : null}

      <MomentRail
        items={railItems}
        currentMs={currentMs}
        selectedItemId={selectedRailItemId}
        onSelect={onSelectRailItem}
        formatClock={formatClock}
        reducedMotion={reducedMotion}
      />

      <StudioClipSequence
        clips={clipSequence}
        activeClipId={activeClipId}
        onSelect={onSelectClip}
        formatClock={formatClock}
      />

      <StudioTranscript
        segments={transcriptSegments}
        activeSegmentIndex={activeSegmentIndex}
        onSeek={handleSeek}
        formatClock={formatClock}
        reducedMotion={reducedMotion}
        viewMode={view}
        autoScrollOnLoad={autoScrollOnLoad}
      />

      {/* Overlays (citation dialog + copy feedback) render INSIDE the shell so the
          studio palette (data-studio-theme) cascades to them (review #9). Their
          own position:fixed still floats them above the page. */}
      {overlaySlot}
    </main>
  )
}
