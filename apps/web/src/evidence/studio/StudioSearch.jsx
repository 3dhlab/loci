import { useCallback, useMemo, useRef, useState } from 'react'

import SearchAutosuggest from '../../components/SearchAutosuggest'
import { searchPublicSegments } from '../../lib/api'
import {
  availableStudioFilters,
  filterStudioResults,
  isOnPageResult,
  isSafeEvidenceUrl,
  STUDIO_SEARCH_GROUP_CAP,
} from '../../lib/studioSearchGrouping'

// Studio P4 — persistent search inside the Studio (?studio=1 only). Wraps the
// existing SearchAutosuggest (ghost + ARIA combobox preserved) and, on submit,
// runs ONE collection-wide public segment search, then presents bounded results
// grouped into "On this page" (verb: Jump, seek in place) and "Across the
// collection" (verb: Open, navigate). Filters: All / This session / This
// collection / Same object, degrading when a result field is unavailable.
//
// Public search endpoint only; results render as text (no raw HTML); stale
// responses are dropped via a monotonic request id; the segment query runs only
// on explicit submit (suggestions are debounced/aborted inside SearchAutosuggest).

const RESULT_FETCH_LIMIT = 24

function defaultFormatClock(ms) {
  const total = Math.max(0, Math.floor(Number(ms || 0) / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function resultTime(result) {
  const start = Number(result?.start_ms)
  if (Number.isFinite(start) && start >= 0) return start
  const ctx = Number(result?.context_start_ms)
  return Number.isFinite(ctx) && ctx >= 0 ? ctx : 0
}

function resultSnippet(result) {
  const raw = result?.snippet ?? result?.text ?? result?.context_text ?? ''
  return typeof raw === 'string' ? raw.trim() : ''
}

function ResultRow({ result, verb, onActivate, formatClock }) {
  const title = result?.object_name || result?.video_title || 'Untitled object'
  const snippet = resultSnippet(result)
  return (
    <li className="studio-search-result" role="option" aria-selected={false}>
      <button type="button" className={`studio-search-verb studio-search-verb-${verb.toLowerCase()}`} onClick={() => onActivate(result)}>
        {verb}
      </button>
      <span className="studio-search-result-body">
        <span className="studio-search-result-title">{title}</span>
        {snippet ? <span className="studio-search-result-snippet">{snippet}</span> : null}
      </span>
      <time className="studio-search-result-time">{formatClock(resultTime(result))}</time>
    </li>
  )
}

export default function StudioSearch({
  currentVideoStableId = '',
  currentObjectPublicId = '',
  currentProjectSlug = '',
  onJump,
  onOpen,
  formatClock = defaultFormatClock,
}) {
  const current = useMemo(
    () => ({ videoStableId: currentVideoStableId, objectPublicId: currentObjectPublicId, projectSlug: currentProjectSlug }),
    [currentVideoStableId, currentObjectPublicId, currentProjectSlug],
  )
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [activeFilter, setActiveFilter] = useState('all')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hasRun, setHasRun] = useState(false)
  const requestIdRef = useRef(0)
  const inputRef = useRef(null)

  // Close the suggestion dropdown + clear ghost after a submit so it never
  // overlaps the results panel (blur drives SearchAutosuggest's close + ghost
  // dismissal).
  const dismissSuggestions = useCallback(() => {
    const node = inputRef.current
    if (node && typeof node.blur === 'function') {
      node.blur()
    }
  }, [])

  const resetResults = useCallback(() => {
    requestIdRef.current += 1 // invalidate any in-flight response
    setResults([])
    setSubmittedQuery('')
    setActiveFilter('all')
    setError('')
    setHasRun(false)
    setLoading(false)
  }, [])

  const runSearch = useCallback(async (rawQuery) => {
    const q = String(rawQuery ?? '').trim()
    if (!q) {
      resetResults()
      return
    }
    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId
    setLoading(true)
    setError('')
    setHasRun(true)
    setSubmittedQuery(q)
    setActiveFilter('all')
    try {
      const payload = await searchPublicSegments({ query: q, retrieval_mode: 'combined', limit: RESULT_FETCH_LIMIT })
      if (requestIdRef.current !== requestId) return
      setResults(Array.isArray(payload?.results) ? payload.results : [])
    } catch (err) {
      if (requestIdRef.current !== requestId) return
      setResults([])
      setError('Search is unavailable right now. Please try again.')
    } finally {
      if (requestIdRef.current === requestId) setLoading(false)
    }
  }, [resetResults])

  const handleJump = useCallback((result) => {
    // Seek in place — preserve the current object/session; close the results.
    onJump?.(result)
  }, [onJump])

  const handleOpen = useCallback((result) => {
    if (!isSafeEvidenceUrl(result?.evidence_url)) {
      return
    }
    resetResults() // clear ghost/results before navigating away
    onOpen?.(result)
  }, [onOpen, resetResults])

  const filters = useMemo(() => availableStudioFilters(results, current), [results, current])
  const filtered = useMemo(() => filterStudioResults(results, activeFilter, current), [results, activeFilter, current])
  // Pre-cap group totals so the heading can say "showing 6 of N" (understandable
  // truncation) rather than silently dropping matches.
  const { onPageFull, collectionFull } = useMemo(() => {
    const onp = []
    const col = []
    for (const r of filtered) {
      if (isOnPageResult(r, current)) onp.push(r)
      else col.push(r)
    }
    return { onPageFull: onp, collectionFull: col }
  }, [filtered, current])
  const onPage = onPageFull.slice(0, STUDIO_SEARCH_GROUP_CAP)
  const collection = collectionFull.slice(0, STUDIO_SEARCH_GROUP_CAP)
  const showPanel = hasRun && Boolean(submittedQuery)
  const totalMatched = onPageFull.length + collectionFull.length

  const groupCountLabel = (shown, total) => (total > shown ? `showing ${shown} of ${total}` : `${total}`)

  return (
    <section className="studio-search" aria-label="Search moments">
      <form
        className="studio-search-form"
        role="search"
        onSubmit={(event) => {
          event.preventDefault()
          runSearch(query)
          dismissSuggestions()
        }}
      >
        <SearchAutosuggest
          value={query}
          onChange={setQuery}
          onSubmit={(phrase) => { runSearch(phrase); dismissSuggestions() }}
          onClear={resetResults}
          inputRef={inputRef}
          token={null}
          placeholder="Search moments in this object or across the collection"
          clearLabel="Clear search"
          inputProps={{ 'aria-label': 'Search moments', enterKeyHint: 'search' }}
        />
      </form>

      {showPanel ? (
        <div className="studio-search-panel">
          <div className="studio-search-toolbar">
            <div className="studio-search-filters" role="group" aria-label="Filter results">
              {filters.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`studio-search-filter${activeFilter === f.key ? ' is-active' : ''}`}
                  aria-pressed={activeFilter === f.key}
                  onClick={() => setActiveFilter(f.key)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <p className="studio-search-status" role="status" aria-live="polite">
              {loading
                ? 'Searching…'
                : error
                  ? error
                  : `${totalMatched} moment${totalMatched === 1 ? '' : 's'} for “${submittedQuery}”`}
            </p>
          </div>

          {!loading && !error && totalMatched === 0 ? (
            <p className="studio-search-empty muted">No moments found for “{submittedQuery}”.</p>
          ) : null}

          {/* Single scroll container for both groups — no nested per-group
              scrollbars (fix P4.1 #6). */}
          {totalMatched > 0 ? (
            <div className="studio-search-results">
              {onPage.length ? (
                <section className="studio-search-group" aria-label="On this page">
                  <h3 className="studio-search-group-heading">
                    On this page <span className="studio-search-group-count">{groupCountLabel(onPage.length, onPageFull.length)}</span>
                  </h3>
                  <ul className="studio-search-result-list" role="listbox" aria-label="On this page results">
                    {onPage.map((result) => (
                      <ResultRow key={result.segment_id} result={result} verb="Jump" onActivate={handleJump} formatClock={formatClock} />
                    ))}
                  </ul>
                </section>
              ) : null}

              {collection.length ? (
                <section className="studio-search-group" aria-label="Across the collection">
                  <h3 className="studio-search-group-heading">
                    Across the collection <span className="studio-search-group-count">{groupCountLabel(collection.length, collectionFull.length)}</span>
                  </h3>
                  <ul className="studio-search-result-list" role="listbox" aria-label="Across the collection results">
                    {collection.map((result) => (
                      <ResultRow key={result.segment_id} result={result} verb="Open" onActivate={handleOpen} formatClock={formatClock} />
                    ))}
                  </ul>
                </section>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
