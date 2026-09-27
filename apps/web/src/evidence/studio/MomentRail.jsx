import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { centeredRailScrollLeft } from '../../lib/evidenceScroll'

// Studio P3 — moment rail. A horizontal filmstrip of seek targets built from the
// page's published annotations (moments) and standalone clips (auto-clips).
// Selecting an item seeks the shared video clock through the host's `onSelect`
// (the rail never touches the clock directly). The now-playing item is derived
// from `currentMs` so it tracks playback, independent of the last user pick.
//
// Two-color moment/auto-clip distinction is also encoded non-visually (each
// option's aria-label says "Moment" or "Auto clip") so it does not rely on color
// alone (WCAG 1.4.1). The rail is a listbox with roving tabindex.

const KIND_LABEL = { moment: 'Moment', clip: 'Auto clip' }

// Stable 16:9 poster thumbnail with a graceful fallback when the image is
// missing or fails to load (the derived clip-poster endpoint 404s / no clip).
function RailThumbnail({ src, kindLabel }) {
  const [failed, setFailed] = useState(false)
  const showImage = Boolean(src) && !failed
  return (
    <span className="studio-rail-thumb" aria-hidden="true">
      {showImage ? (
        <img
          className="studio-rail-thumb-img"
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="studio-rail-thumb-fallback">{kindLabel}</span>
      )}
    </span>
  )
}

function defaultFormatClock(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

// The item whose [startMs, endMs) window contains `currentMs`. On overlap the
// latest-starting window wins (most specific), and a moment beats an auto-clip so
// the pulse lands on the curated moment when both cover the same instant.
function findNowPlayingId(items, currentMs) {
  const ms = Number(currentMs) || 0
  let best = null
  for (const item of items) {
    const start = Number(item.startMs) || 0
    const end = Number.isFinite(item.endMs) ? Number(item.endMs) : start
    if (ms < start || ms >= Math.max(start + 1, end)) {
      continue
    }
    if (
      best === null
      || start > best.start
      || (start === best.start && item.kind === 'moment' && best.kind !== 'moment')
    ) {
      best = { id: item.id, start, kind: item.kind }
    }
  }
  return best ? best.id : ''
}

export default function MomentRail({
  items = [],
  currentMs = 0,
  selectedItemId = '',
  onSelect,
  formatClock = defaultFormatClock,
  reducedMotion = false,
}) {
  const optionRefs = useRef(new Map())
  const trackRef = useRef(null)
  const nowPlayingId = useMemo(() => findNowPlayingId(items, currentMs), [items, currentMs])

  // Roving tabindex anchor: the selected item, else the now-playing item, else
  // the first item. Keyboard focus moves between options without leaving the rail.
  const tabbableId = useMemo(() => {
    if (selectedItemId && items.some((item) => item.id === selectedItemId)) {
      return selectedItemId
    }
    if (nowPlayingId) {
      return nowPlayingId
    }
    return items.length ? items[0].id : ''
  }, [items, nowPlayingId, selectedItemId])

  const focusItem = useCallback((id) => {
    const node = optionRefs.current.get(id)
    if (node instanceof HTMLElement) {
      node.focus()
    }
  }, [])

  const handleKeyDown = useCallback((event, index) => {
    let nextIndex = null
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        nextIndex = (index + 1) % items.length
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        nextIndex = (index - 1 + items.length) % items.length
        break
      case 'Home':
        nextIndex = 0
        break
      case 'End':
        nextIndex = items.length - 1
        break
      default:
        return
    }
    event.preventDefault()
    const next = items[nextIndex]
    if (next) {
      focusItem(next.id)
    }
  }, [focusItem, items])

  useEffect(() => {
    const live = new Set(items.map((item) => item.id))
    for (const key of optionRefs.current.keys()) {
      if (!live.has(key)) {
        optionRefs.current.delete(key)
      }
    }
  }, [items])

  // Center the selected (else now-playing) item inside the rail viewport. A
  // rail-local scroll keeps the document's vertical position unchanged.
  const activeItemId = (selectedItemId && items.some((i) => i.id === selectedItemId)) ? selectedItemId : nowPlayingId
  useEffect(() => {
    if (!activeItemId) {
      return
    }
    const track = trackRef.current
    const node = optionRefs.current.get(activeItemId)
    if (!(track instanceof HTMLElement) || !(node instanceof HTMLElement)) {
      return
    }
    const left = centeredRailScrollLeft({
      itemOffsetLeft: node.offsetLeft,
      itemOffsetWidth: node.offsetWidth,
      trackClientWidth: track.clientWidth,
      trackScrollWidth: track.scrollWidth,
    })
    if (typeof track.scrollTo === 'function') {
      track.scrollTo({ left, behavior: reducedMotion ? 'auto' : 'smooth' })
      return
    }
    track.scrollLeft = left
  }, [activeItemId, reducedMotion])

  if (!items.length) {
    return null
  }

  return (
    <section className="studio-rail" aria-label="Moments and clips">
      <h2 className="studio-rail-heading">Moments</h2>
      <div
        ref={trackRef}
        className={`studio-rail-track${reducedMotion ? ' is-reduced-motion' : ''}`}
        role="listbox"
        aria-label="Seek to a moment or clip"
        aria-orientation="horizontal"
      >
        {items.map((item, index) => {
          const isSelected = item.id === selectedItemId
          const isPlaying = item.id === nowPlayingId
          const kindLabel = KIND_LABEL[item.kind] || 'Moment'
          const timeLabel = formatClock(item.startMs)
          return (
            <button
              key={item.id}
              ref={(node) => {
                if (node) {
                  optionRefs.current.set(item.id, node)
                  return
                }
                optionRefs.current.delete(item.id)
              }}
              type="button"
              role="option"
              aria-selected={isSelected}
              aria-current={isPlaying ? 'true' : undefined}
              aria-label={`${kindLabel}: ${item.label || timeLabel}, at ${timeLabel}`}
              tabIndex={item.id === tabbableId ? 0 : -1}
              data-kind={item.kind}
              className={`studio-rail-item${isSelected ? ' is-selected' : ''}${isPlaying ? ' is-playing' : ''}`}
              onClick={() => onSelect?.(item)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  onSelect?.(item)
                  return
                }
                handleKeyDown(event, index)
              }}
            >
              <RailThumbnail src={item.posterUrl} kindLabel={kindLabel} />
              <span className="studio-rail-item-meta">
                <span className="studio-rail-item-kind" aria-hidden="true">{kindLabel}</span>
                <span className="studio-rail-item-title">{item.label || timeLabel}</span>
                <time className="studio-rail-item-time">{timeLabel}</time>
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
