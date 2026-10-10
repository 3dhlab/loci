import { semanticFitTier } from '../lib/searchPresentation'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import ErrorState, { buildErrorState } from '../components/ErrorState'
import PublicStatsPanel from '../components/PublicStatsPanel'
import SearchAutosuggest from '../components/SearchAutosuggest'
import { getPublicObjectsPage, getPublicProjects, resolveApiUrl, resolvePublicObjectPosterUrl, searchPublicSegments } from '../lib/api'
import BrandLockup from '../components/BrandLockup'
import BrandMotion from '../components/BrandMotion'
import { PUBLIC_BROWSE_PATH, currentLocationKey, ensureLocationChangeEvents, navigateToPublicBrowse, navigateToUrl, subscribeToLocationChanges } from '../lib/navigation'
import { buildPublicBrowsePageUrl, getPublicObjectPageButtons, normalizePublicObjectPageResponse, PUBLIC_OBJECT_PAGE_SIZE, readPublicObjectPage } from '../lib/publicObjectPager'
import { setPageMeta } from '../lib/seo'
import './publicObjectPager.css'
import { StudioSurfaceChrome, isStudioSurfaceEnabled, useStudioSurfaceTheme } from './studioSurface'

const PUBLIC_MAIN_CONTENT_ID = 'public-main-content'

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

function buildMotionDelayStyle(index, stepMs = 40, maxDelayMs = 160) {
  const resolvedDelay = Math.min(Math.max(index, 0) * stepMs, maxDelayMs)
  return {
    '--motion-delay': `${resolvedDelay}ms`
  }
}

function normalizeOptionalText(value) {
  if (typeof value !== 'string') {
    return ''
  }

  const trimmed = value.trim()
  return trimmed || ''
}

function normalizeFiniteNumber(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function normalizeEvidenceUrl(value) {
  if (typeof value !== 'string') {
    return ''
  }

  const trimmed = value.trim()
  return trimmed || ''
}

function navigateToEvidenceUrl(value) {
  if (typeof window === 'undefined') {
    return false
  }

  const normalized = normalizeEvidenceUrl(value)
  if (!normalized) {
    return false
  }

  let target
  try {
    target = new URL(normalized, window.location.href)
  } catch {
    return false
  }

  if (target.origin !== window.location.origin || !target.pathname.startsWith('/evidence/objects/')) {
    return false
  }

  window.location.assign(`${target.pathname}${target.search}${target.hash}`)
  return true
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function resultHasSceneSupport(result) {
  if (!result || typeof result !== 'object') {
    return false
  }

  return result.visual_score != null
    || Boolean(normalizeOptionalText(result.scene_description))
    || Boolean(normalizeOptionalText(result.thumbnail_url))
}

function searchSceneSupportText(result) {
  const sceneDescription = normalizeOptionalText(result?.scene_description)
  if (sceneDescription) {
    return sceneDescription
  }

  if (!resultHasSceneSupport(result)) {
    return ''
  }

  return 'Visual support is available for this transcript moment.'
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

function surroundingContextRange(result) {
  const momentStart = normalizeFiniteNumber(result?.start_ms) ?? 0
  const momentEnd = Math.max(momentStart, normalizeFiniteNumber(result?.end_ms) ?? momentStart)
  const contextStart = Math.max(0, normalizeFiniteNumber(result?.context_start_ms) ?? momentStart)
  const contextEnd = Math.max(contextStart, normalizeFiniteNumber(result?.context_end_ms) ?? momentEnd)

  return {
    momentStart,
    momentEnd,
    contextStart,
    contextEnd,
  }
}

function searchMatchBasisLine(result) {
  return resultHasSceneSupport(result)
    ? 'Matched in the transcript with scene support.'
    : 'Matched in the transcript.'
}

function searchResultContextDetails(result) {
  return [
    { label: 'Object', value: normalizeOptionalText(result?.object_name) },
    { label: 'Collection', value: normalizeOptionalText(result?.project_name) },
  ].filter((detail) => detail.value)
}

function browseSummary(objectRow) {
  const authoredSummary = normalizeOptionalText(objectRow?.public_standfirst)
    || normalizeOptionalText(objectRow?.description)

  if (authoredSummary) {
    return authoredSummary
  }

  if (objectRow?.evidence_url) {
    return 'Linked evidence is available for this object.'
  }

  return 'This object is part of the public collection and can be opened through its collection record.'
}

const PUBLIC_SEARCH_PAGE_SIZE = 5

function initialSearchMeta() {
  return {
    totalResults: 0,
    page: 1,
    pageSize: PUBLIC_SEARCH_PAGE_SIZE,
    totalPages: 0,
    resultWindow: 0,
    resultWindowCapped: false,
  }
}

function readInitialSearchFormFromLocation() {
  if (typeof window === 'undefined') {
    return { query: '', project_id: '' }
  }

  const params = new URLSearchParams(window.location.search || '')
  const query = normalizeOptionalText(params.get('q')) || normalizeOptionalText(params.get('query'))
  const projectId = normalizeOptionalText(params.get('project_id')) || normalizeOptionalText(params.get('project'))

  return {
    query,
    project_id: projectId,
  }
}

function buildPublicSearchUrl(query, projectId, requestedObjectPage) {
  const normalizedQuery = normalizeOptionalText(query)
  const normalizedProjectId = normalizeOptionalText(projectId)
  const params = new URLSearchParams()
  if (normalizedQuery) {
    params.set('q', normalizedQuery)
  }
  if (normalizedProjectId) {
    params.set('project_id', normalizedProjectId)
  }
  const objectPage = requestedObjectPage ?? (typeof window === 'undefined' ? 1 : readPublicObjectPage(window.location.search))
  if (objectPage > 1) params.set('object_page', String(objectPage))

  const serializedParams = params.toString()
  return `${PUBLIC_BROWSE_PATH}${serializedParams ? `?${serializedParams}` : ''}`
}

function syncPublicSearchUrl(query, projectId, { replace = true, objectPage } = {}) {
  if (typeof window === 'undefined') {
    return
  }

  const targetUrl = buildPublicSearchUrl(query, projectId, objectPage)

  if (targetUrl === PUBLIC_BROWSE_PATH) {
    navigateToPublicBrowse({ replace })
    return
  }

  navigateToUrl(targetUrl, { replace })
}

function ResultThumbnail({ src, alt, fallbackTitle, fallbackNote }) {
  const [failed, setFailed] = useState(false)
  const resolvedSrc = normalizeOptionalText(src)

  if (!resolvedSrc || failed) {
    return (
      <div className="search-result-thumbnail-placeholder media-frame-4x3" role="img" aria-label={fallbackTitle}>
        <strong>{fallbackTitle}</strong>
        <span>{fallbackNote}</span>
      </div>
    )
  }

  return (
    <img
      src={resolvedSrc}
      alt={alt}
      className="search-result-thumbnail media-frame-4x3"
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  )
}

function BrowseCardVisual({ variant = 'evidence' }) {
  return (
    <div
      className={`public-library-card-visual public-library-card-visual-placeholder ${variant}`.trim()}
      aria-hidden="true"
    >
      <div className="public-library-card-visual-aura" aria-hidden="true" />
      <div className="public-library-card-visual-mark" aria-hidden="true">
        <BrandMotion
          name={variant === 'evidence' ? 'pivot' : 'scan'}
          size={variant === 'evidence' ? 88 : 80}
          className="brand-motion-glow"
        />
      </div>
    </div>
  )
}

function ReadyCardPoster({ posterUrl, objectName }) {
  const [failed, setFailed] = useState(false)
  const [timedOut, setTimedOut] = useState(false)
  const [imageReady, setImageReady] = useState(false)
  const resolvedPosterUrl = normalizeOptionalText(posterUrl)
  const alt = `${objectName} poster`

  useEffect(() => {
    setFailed(false)
    setTimedOut(false)
    setImageReady(false)
    if (!resolvedPosterUrl) return undefined
    const timeoutId = window.setTimeout(() => setTimedOut(true), 8000)
    return () => window.clearTimeout(timeoutId)
  }, [resolvedPosterUrl])

  return (
    <div className={`public-library-card-visual public-library-card-poster-frame ${imageReady ? 'is-loaded' : 'is-pending'}`}>
      {resolvedPosterUrl && !failed ? <img
        src={resolvedPosterUrl}
        alt={alt}
        className="public-library-card-poster-image"
        loading="eager"
        decoding="async"
        onLoad={(event) => {
          const image = event.currentTarget
          if (typeof image.decode === 'function') image.decode().then(() => setImageReady(true), () => setFailed(true))
          else setImageReady(true)
        }}
        onError={() => setFailed(true)}
      /> : null}
      {!imageReady ? (
        <div className="public-library-card-poster-placeholder" role="img" aria-label={alt}>
          <strong>{failed || !resolvedPosterUrl ? 'Poster unavailable' : 'Poster loading'}</strong>
          <span>{!resolvedPosterUrl
            ? 'A published poster has not been generated yet.'
            : failed
              ? 'The published poster could not be loaded.'
              : timedOut
                ? 'The poster is taking longer to load. Object details remain available.'
                : 'Object details are available while the poster loads.'}</span>
        </div>
      ) : null}
    </div>
  )
}

function ReadyCardPosterLink({ objectRow, label, onOpen, posterUrl }) {
  const evidenceUrl = normalizeEvidenceUrl(objectRow?.evidence_url)

  if (!evidenceUrl) {
    return <ReadyCardPoster posterUrl={posterUrl} objectName={objectRow?.name || 'Published object'} />
  }

  return (
    <a
      className="public-library-card-poster-link"
      href={evidenceUrl}
      aria-label={label}
      onClick={(event) => {
        event.preventDefault()
        onOpen(objectRow)
      }}
    >
      <ReadyCardPoster posterUrl={posterUrl} objectName={objectRow?.name || 'Published object'} />
    </a>
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

function SearchLoadingState({ query }) {
  const searchLabel = normalizeOptionalText(query)

  return (
    <div className="public-library-search-loading motion-browse-entry" role="status" aria-live="polite">
      <div className="public-library-search-loading-mark" aria-hidden="true">
        <BrandMotion name="scan" size={58} className="brand-motion-glow" />
      </div>
      <div className="public-library-search-loading-copy">
        <p className="kicker">Searching transcript moments</p>
        <strong>{searchLabel ? `Finding matches for "${searchLabel}"` : 'Finding matching evidence'}</strong>
        <p className="muted">Preparing transcript lines, visual support, and evidence links.</p>
      </div>
      <div className="public-library-search-loading-pulse" aria-hidden="true">
        <span />
      </div>
    </div>
  )
}

function buildBrowseLoadErrorState(error) {
  return buildErrorState(error, {
    fallbackKind: 'network',
    networkDetail: 'The public collection is temporarily unavailable.',
    notFoundDetail: 'The published collection could not be found.',
    unauthorizedDetail: 'This public collection requires authorization.'
  })
}

function buildBrowseSearchErrorState(error) {
  return buildErrorState(error, {
    fallbackKind: 'network',
    networkDetail: 'Try again in a moment or clear your query.',
    notFoundDetail: 'This published search is no longer available.',
    unauthorizedDetail: 'This published search requires authorization.'
  })
}

function reloadProtectedPage() {
  window.location.assign(window.location.href)
}

export default function PublicBrowseApp() {
  // P4.9 — Studio is the default public browse UI after visual approval.
  // `?studio=0` / `?legacy=1` keeps the legacy browse available for rollback.
  const studioEnabledRef = useRef(isStudioSurfaceEnabled(undefined, { defaultEnabled: true }))
  const studioEnabled = studioEnabledRef.current
  const { palette, toggle } = useStudioSurfaceTheme()
  const initialSearchFormRef = useRef(readInitialSearchFormFromLocation())
  const hasInitialRoutedSearch = Boolean(normalizeOptionalText(initialSearchFormRef.current.query))
  const [projects, setProjects] = useState([])
  const [objects, setObjects] = useState([])
  const [objectPage, setObjectPage] = useState(() => readPublicObjectPage(typeof window === 'undefined' ? '' : window.location.search))
  const [objectPageMeta, setObjectPageMeta] = useState({ page: 1, pageSize: PUBLIC_OBJECT_PAGE_SIZE, total: 0, totalPages: 0 })
  const [objectPageError, setObjectPageError] = useState(null)
  const [objectsLoading, setObjectsLoading] = useState(true)
  const [pageDirection, setPageDirection] = useState('forward')
  const [loading, setLoading] = useState(true)
  const [browseError, setBrowseError] = useState(null)
  const [activeProjectId, setActiveProjectId] = useState(() => initialSearchFormRef.current.project_id || '')
  const [searchForm, setSearchForm] = useState(() => initialSearchFormRef.current)
  const [searchLoading, setSearchLoading] = useState(hasInitialRoutedSearch)
  const [searchResults, setSearchResults] = useState([])
  const [searchError, setSearchError] = useState(null)
  const [searchMeta, setSearchMeta] = useState(() => initialSearchMeta())
  const [hasSearched, setHasSearched] = useState(hasInitialRoutedSearch)
  const searchInputRef = useRef(null)
  const browseSectionRef = useRef(null)
  const browseRequestIdRef = useRef(0)
  const browsePageRequestIdRef = useRef(0)
  const lastBrowsePageRef = useRef(objectPage)
  const searchRequestIdRef = useRef(0)
  const initialSearchExecutedRef = useRef(false)

  const loadBrowseSurface = useCallback(async () => {
    const requestId = browseRequestIdRef.current + 1
    browseRequestIdRef.current = requestId
    setLoading(true)
    setBrowseError(null)

    try {
      const projectRows = await getPublicProjects()

      if (browseRequestIdRef.current !== requestId) {
        return
      }

      setProjects(projectRows)
    } catch (error) {
      if (browseRequestIdRef.current !== requestId) {
        return
      }

      setBrowseError(buildBrowseLoadErrorState(error))
    } finally {
      if (browseRequestIdRef.current === requestId) {
        setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    loadBrowseSurface()

    return () => {
      browseRequestIdRef.current += 1
    }
  }, [loadBrowseSurface])

  const loadObjectPage = useCallback(async (page = objectPage, projectId = activeProjectId) => {
    const requestId = browsePageRequestIdRef.current + 1
    browsePageRequestIdRef.current = requestId
    setObjectPageError(null)
    setObjectsLoading(true)
    try {
      const response = await getPublicObjectsPage({ projectId: projectId || undefined, page, pageSize: PUBLIC_OBJECT_PAGE_SIZE })
      if (browsePageRequestIdRef.current !== requestId) return
      const normalized = normalizePublicObjectPageResponse(response)
      setObjects(normalized.items)
      setObjectPageMeta(normalized)
      lastBrowsePageRef.current = normalized.page
      if (normalized.page !== page) {
        setObjectPage(normalized.page)
        const targetUrl = buildPublicBrowsePageUrl({ search: window.location.search, page: normalized.page, projectId })
        ensureLocationChangeEvents()
        window.history.replaceState(null, '', targetUrl)
      }
    } catch (error) {
      if (browsePageRequestIdRef.current !== requestId) return
      setObjectPageError(buildBrowseLoadErrorState(error))
    } finally {
      if (browsePageRequestIdRef.current === requestId) setObjectsLoading(false)
    }
  }, [activeProjectId, objectPage])

  useEffect(() => {
    void loadObjectPage(objectPage, activeProjectId)
    return () => {
      browsePageRequestIdRef.current += 1
    }
  }, [loadObjectPage, objectPage, activeProjectId])

  useEffect(() => subscribeToLocationChanges(() => {
    const params = new URLSearchParams(window.location.search)
    const nextProjectId = normalizeOptionalText(params.get('project_id')) || normalizeOptionalText(params.get('project'))
    const nextPage = readPublicObjectPage(window.location.search)
    const currentPage = lastBrowsePageRef.current
    if (nextPage > currentPage) setPageDirection('forward')
    else if (nextPage < currentPage) setPageDirection('backward')
    setActiveProjectId(nextProjectId)
    if (nextPage !== lastBrowsePageRef.current) setObjectPage(nextPage)
  }), [])

  const projectById = useMemo(
    () => new Map(projects.map((project) => [project.id, project])),
    [projects]
  )

  const activeProject = useMemo(
    () => (activeProjectId ? projectById.get(activeProjectId) || null : null),
    [activeProjectId, projectById]
  )

  const browseMetaImage = useMemo(() => {
    const thumbnailPath = normalizeOptionalText(
      searchResults.find((result) => normalizeOptionalText(result.thumbnail_url))?.thumbnail_url || ''
    )
    return thumbnailPath ? resolvePublicObjectPosterUrl(thumbnailPath) : ''
  }, [searchResults])

  const searchResultsStart = searchMeta.totalResults
    ? ((searchMeta.page - 1) * searchMeta.pageSize) + 1
    : 0
  const searchResultsEnd = searchMeta.totalResults
    ? searchResultsStart + searchResults.length - 1
    : 0
  const searchResultsSummary = searchMeta.totalResults
    ? searchMeta.resultWindowCapped
      ? `Showing ${searchResultsStart}-${searchResultsEnd} of top ${searchMeta.resultWindow} matching moments.`
      : `Showing ${searchResultsStart}-${searchResultsEnd} of ${searchMeta.totalResults} matching moments.`
    : ''
  const activeSearchQuery = normalizeOptionalText(searchForm.query)
  const hasActiveSearchState = Boolean(
    hasSearched
    || searchResults.length
    || searchError
    || searchLoading
  )
  const hasActiveQuery = Boolean(activeSearchQuery) && hasActiveSearchState

  useEffect(() => {
    const projectName = normalizeOptionalText(activeProject?.name)
    const title = projectName || 'Explore published objects'
    const description = activeSearchQuery && hasSearched
      ? `Search published objects and evidence moments for "${activeSearchQuery}".`
      : projectName
        ? `Browse published objects and linked evidence moments for ${projectName}.`
        : 'Evidence, located. Browse published objects, linked moments, and synchronized transcript context.'

    setPageMeta({
      title,
      description,
      image: browseMetaImage,
      url: window.location.href,
      updateDocumentTitle: true,
    })
  }, [activeProject?.name, activeSearchQuery, browseMetaImage, hasSearched])

  function renderSearchPagination(location = 'top') {
    if (searchMeta.totalPages <= 1) {
      return null
    }

    return (
      <div className={`search-pagination search-pagination-${location}`.trim()} aria-label="Public search result pages">
        <button
          type="button"
          className="ghost search-pagination-button"
          onClick={() => changeSearchPage(searchMeta.page - 1)}
          disabled={searchLoading || searchMeta.page <= 1}
        >
          Previous
        </button>
        <span className="search-pagination-status">Page {searchMeta.page} of {searchMeta.totalPages}</span>
        <button
          type="button"
          className="ghost search-pagination-button"
          onClick={() => changeSearchPage(searchMeta.page + 1)}
          disabled={searchLoading || searchMeta.page >= searchMeta.totalPages}
        >
          Next
        </button>
      </div>
    )
  }

  async function executeSearch(page = 1, options = {}) {
    const { query: queryOverride, replaceUrl = true, objectPage: objectPageOverride } = options
    const hasProjectIdOverride = Object.prototype.hasOwnProperty.call(options, 'projectId')
    const query = String(queryOverride ?? searchForm.query ?? '').trim()
    const projectId = hasProjectIdOverride
      ? (normalizeOptionalText(options.projectId) || undefined)
      : (activeProjectId || undefined)

    if (!query) {
      if (hasActiveSearchState) {
        clearSearch()
        return
      }

      setSearchError(null)
      return
    }

    const targetUrl = buildPublicSearchUrl(query, projectId, objectPageOverride)
    if (targetUrl !== currentLocationKey()) {
      syncPublicSearchUrl(query, projectId, { replace: replaceUrl })
      return
    }

    setHasSearched(true)
    setSearchError(null)

    const requestId = searchRequestIdRef.current + 1
    searchRequestIdRef.current = requestId
    setSearchLoading(true)
    try {
      const response = await searchPublicSegments({
        query,
        project_id: projectId,
        retrieval_mode: 'combined',
        page,
        page_size: PUBLIC_SEARCH_PAGE_SIZE,
      })

      if (searchRequestIdRef.current !== requestId) {
        return
      }

      setSearchResults((response.results || []).map((result) => ({
        ...result,
        matched_query: query,
      })))
      setSearchMeta({
        totalResults: Number(response.total_results || 0),
        page: Number(response.page || page),
        pageSize: Number(response.page_size || PUBLIC_SEARCH_PAGE_SIZE),
        totalPages: Number(response.total_pages || 0),
        resultWindow: Number(response.result_window || 0),
        resultWindowCapped: Boolean(response.result_window_capped),
      })
    } catch (error) {
      if (searchRequestIdRef.current !== requestId) {
        return
      }

      setSearchResults([])
      setSearchMeta(initialSearchMeta())
      setSearchError(buildBrowseSearchErrorState(error))
    } finally {
      if (searchRequestIdRef.current === requestId) {
        setSearchLoading(false)
      }
    }
  }

  useEffect(() => {
    if (initialSearchExecutedRef.current) {
      return
    }

    initialSearchExecutedRef.current = true
    const initialSearch = initialSearchFormRef.current
    const query = normalizeOptionalText(initialSearch.query)
    if (!query) {
      return
    }

    void executeSearch(1, {
      query,
      projectId: initialSearch.project_id || undefined,
      replaceUrl: true,
    })
  }, [])

  async function handleSearch(event) {
    event.preventDefault()
    await executeSearch(1)
  }

  function changeSearchPage(nextPage) {
    if (searchLoading || nextPage < 1 || (searchMeta.totalPages && nextPage > searchMeta.totalPages)) {
      return
    }

    executeSearch(nextPage)
  }

  function clearSearch({ projectId = activeProjectId } = {}) {
    searchRequestIdRef.current += 1
    setSearchLoading(false)
    setSearchForm((current) => ({ ...current, query: '' }))
    setSearchResults([])
    setSearchError(null)
    setSearchMeta(initialSearchMeta())
    setHasSearched(false)
    syncPublicSearchUrl('', projectId, { replace: true })
  }

  function handleSearchInputChange(nextQuery) {
    const normalizedQuery = String(nextQuery ?? '')

    if (!normalizedQuery.trim() && hasActiveSearchState) {
      clearSearch()
      return
    }

    setSearchForm((current) => ({ ...current, query: normalizedQuery }))
  }

  function handleProjectTabChange(nextProjectId) {
    const normalizedProjectId = normalizeOptionalText(nextProjectId)

    if (normalizedProjectId === activeProjectId) {
      return
    }

    setActiveProjectId(normalizedProjectId)
    setObjectPage(1)

    if (hasActiveQuery) {
      void executeSearch(1, {
        query: activeSearchQuery,
        projectId: normalizedProjectId,
        replaceUrl: true,
        objectPage: 1,
      })
      return
    }

    syncPublicSearchUrl('', normalizedProjectId, { replace: true, objectPage: 1 })
  }

  function changeObjectPage(nextPage) {
    if (objectsLoading || nextPage < 1 || (objectPageMeta.totalPages && nextPage > objectPageMeta.totalPages)) return
    if (nextPage === objectPageMeta.page && !objectPageError) return
    setPageDirection(nextPage >= objectPageMeta.page ? 'forward' : 'backward')
    const targetUrl = buildPublicBrowsePageUrl({ search: window.location.search, page: nextPage, projectId: activeProjectId })
    ensureLocationChangeEvents()
    window.history.pushState(null, '', targetUrl)
    lastBrowsePageRef.current = nextPage
    setObjectPage(nextPage)
  }

  function focusSearchInput() {
    const target = searchInputRef.current
    if (!target) {
      return
    }

    target.focus()
    target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' })
  }

  function resetBrowseSurface({ focusSearch = false } = {}) {
    setActiveProjectId('')
    clearSearch({ projectId: '' })

    window.requestAnimationFrame(() => {
      if (focusSearch) {
        focusSearchInput()
        return
      }

      browseSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

  function openObjectEvidence(objectRow) {
    const targetUrl = normalizeEvidenceUrl(objectRow?.evidence_url)
    if (!targetUrl) {
      return
    }

    navigateToEvidenceUrl(targetUrl)
  }

  function openSearchResult(result) {
    const targetUrl = normalizeEvidenceUrl(result?.evidence_url)
    if (!targetUrl) {
      return
    }

    navigateToEvidenceUrl(targetUrl)
  }

  return (
    <>
      <a
        className="skip-link"
        href={`#${PUBLIC_MAIN_CONTENT_ID}`}
        onClick={() => focusSkipTarget(PUBLIC_MAIN_CONTENT_ID)}
      >
        Skip to content
      </a>
      <main
        id={PUBLIC_MAIN_CONTENT_ID}
        tabIndex={-1}
        className={`public-library-screen skip-link-target ${searchResults.length ? 'search-active' : ''} ${studioEnabled ? 'studio-surface' : ''}`.trim()}
        data-studio-theme={studioEnabled ? palette : undefined}
      >
        {studioEnabled ? (
          <StudioSurfaceChrome
            palette={palette}
            onToggle={toggle}
            eyebrow="Loci · Public studio"
            title="Explore published objects"
          />
        ) : null}
        <div className="public-library-shell">
        <header className="public-library-hero">
          <div className="public-library-hero-copy">
            <a
              className="surface-branding surface-branding-link public-library-branding"
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
            <h1>Explore objects through evidence, interpretation, and linked moments.</h1>
            <p className="public-library-hero-text">
              Choose a collection to narrow the objects, then open an evidence page to explore its synchronized video, transcript, and 3D context.
            </p>
          </div>

          {/*
           * Stats panel — Contract 2 of public-landing-session-2026-04-23.md.
           * The shared PublicStatsPanel reads /api/v1/public/stats so this
           * surface and the editorial landing at / render the SAME
           * "X objects currently open for evidence review" sentence from the
           * SAME endpoint. The previous three-card metric block displayed
           * `objects.length` ("published objects") inflated by published
           * rows that were missing one or more cascade gates — visitors
           * would see "5 published objects" while only 1 evidence page
           * actually loaded. That dishonesty pattern is retired here.
           *
           * If the founder later wants the "collections" count visible
           * again, extend PublicStatsPanel with the project count rather
           * than re-inlining a metric card in this component.
           */}
          <div className="public-library-metrics" aria-label="Published collection metrics">
            <PublicStatsPanel />
          </div>
        </header>

        {browseError ? (
          <ErrorState
            kind={browseError.kind}
            detail={browseError.detail}
            kicker="Public browse"
            headingAs="h2"
            className="error-state-inline"
            title={browseError.kind === 'unauthorized' ? 'Published collection is protected' : browseError.kind === 'not_found' ? 'Published collection not found' : 'Public collection unavailable'}
            message={browseError.kind === 'unauthorized' ? 'Access to this public collection requires authorization.' : browseError.kind === 'not_found' ? 'The published collection could not be found.' : 'The public collection is unavailable right now.'}
            primaryActionLabel={browseError.kind === 'unauthorized' ? 'Reload protected page' : 'Try again'}
            onPrimaryAction={browseError.kind === 'unauthorized' ? reloadProtectedPage : loadBrowseSurface}
          />
        ) : null}

        <section className="public-library-search-panel">
          <div className="public-library-section-copy">
            <p className="kicker">Search the collection</p>
            <h2>Find a word, phrase, or recorded moment and open it directly.</h2>
            <p className="muted">
              Search published transcript moments with scene support and move straight into the matching evidence page.
            </p>
          </div>

          <form className="public-library-search-form" onSubmit={handleSearch}>
            <div className="public-library-search-bar">
              <SearchAutosuggest
                value={searchForm.query}
                onChange={handleSearchInputChange}
                onSubmit={(query) => {
                  setSearchForm((current) => ({ ...current, query }))
                  void executeSearch(1, { query })
                }}
                onClear={clearSearch}
                clearLabel="Clear public search"
                projectId={activeProjectId || undefined}
                placeholder="Search published transcript moments"
                inputRef={searchInputRef}
                inputProps={{ 'aria-label': 'Search published transcript moments' }}
              />
              <button type="submit" className="public-library-search-submit" disabled={searchLoading}>{searchLoading ? 'Searching...' : 'Search'}</button>
            </div>
          </form>

          {searchError ? (
            <ErrorState
              kind={searchError.kind}
              detail={searchError.kind === 'network' ? '' : searchError.detail}
              kicker="Search"
              headingAs="h3"
              className="error-state-inline public-library-search-error"
              title={searchError.kind === 'unauthorized' ? 'Search is protected' : searchError.kind === 'not_found' ? 'Search endpoint not found' : 'Search unavailable'}
              message={searchError.kind === 'unauthorized' ? 'Access to this published search requires authorization.' : searchError.kind === 'not_found' ? 'This published search could not be found.' : searchError.detail || 'Try again in a moment or clear your query.'}
              primaryActionLabel={searchError.kind === 'unauthorized' ? 'Reload protected page' : 'Try again'}
              onPrimaryAction={searchError.kind === 'unauthorized' ? reloadProtectedPage : () => executeSearch(searchMeta.page || 1)}
              secondaryActionLabel="Clear search"
              onSecondaryAction={clearSearch}
            />
          ) : null}

          {searchLoading ? (
            <SearchLoadingState query={searchForm.query} />
          ) : searchError ? null : searchResults.length ? (
            <>
              <div className="search-results-toolbar public-library-search-toolbar">
                <div className="search-results-summary">
                  <strong>{searchResultsSummary}</strong>
                  <p className="muted">Results are paged in sets of {searchMeta.pageSize} to keep the public evidence surface compact and scannable.</p>
                </div>
                {renderSearchPagination('top')}
              </div>

              <ul className="list public-library-search-results">
                {searchResults.map((result, index) => {
                  const semanticTier = semanticFitTier(result.semantic_score)
                  const contextWindow = surroundingContextRange(result)
                  const resultActionEnabled = Boolean(result.evidence_url)
                  const resultThumbnailUrl = resolvePublicObjectPosterUrl(result.thumbnail_url || '')
                  const hasSceneSupport = resultHasSceneSupport(result)
                  const sceneSupportText = searchSceneSupportText(result)
                  const resultDetails = searchResultContextDetails(result)
                  const resultActionLabel = `Open evidence at ${formatClock(contextWindow.momentStart)}`

                  return (
                    <li
                      key={result.segment_id}
                      className="search-result-row search-result-shell public-library-search-row motion-browse-entry"
                      style={buildMotionDelayStyle(index, 35, 105)}
                    >
                      <div className="search-result-media">
                        <ResultThumbnail
                          src={resultThumbnailUrl}
                          alt="Representative frame for this public evidence moment"
                          fallbackTitle="Thumbnail unavailable"
                          fallbackNote="This published moment does not have a usable visual frame yet."
                        />
                      </div>
                      <div className="list-main video-list-main search-result-body">
                        <div className="search-result-heading">
                          <div className="search-result-heading-copy">
                            <p className="search-result-kicker">{result.video_title || 'Published evidence moment'}</p>
                            <strong>Transcript moment {formatClock(contextWindow.momentStart)}-{formatClock(contextWindow.momentEnd)}</strong>
                            <span className="search-result-context">
                              Transcript context {formatClock(contextWindow.contextStart)}-{formatClock(contextWindow.contextEnd)}
                            </span>
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
                          disabled={!resultActionEnabled}
                          onClick={() => openSearchResult(result)}
                        >
                          {resultActionLabel}
                        </button>
                      </div>
                    </li>
                  )
                })}
              </ul>

              {renderSearchPagination('bottom')}
            </>
          ) : hasSearched ? (
            <ActionEmptyState
              title="No matching moments"
              text="Try a different search term or collection to find related moments."
              actionLabel="Browse all objects"
              onAction={() => resetBrowseSurface({ focusSearch: false })}
            />
          ) : null}
        </section>

        <section className="public-library-filter-bar" aria-label="Collection filters">
          <button
            type="button"
            className={[activeProjectId ? 'ghost' : '', 'motion-ink-underline'].filter(Boolean).join(' ')}
            onClick={() => handleProjectTabChange('')}
          >
            All collections
          </button>
          {projects.map((project) => (
            <button
              type="button"
              key={project.id}
              className={[activeProjectId === project.id ? '' : 'ghost', 'motion-ink-underline'].filter(Boolean).join(' ')}
              onClick={() => handleProjectTabChange(project.id)}
            >
              {project.name}
            </button>
          ))}
        </section>

        <section className="public-library-section" ref={browseSectionRef}>
          <div className="public-library-section-header">
            <div className="public-library-section-copy">
              <p className="kicker">Browse objects</p>
              <h2>Start with an object, then follow the evidence.</h2>
              <p className="muted">
                {activeProjectId
                  ? `Showing objects from ${projectById.get(activeProjectId)?.name || 'the selected collection'}.`
                  : 'Browse the objects currently available across the public collection.'}
              </p>
            </div>
            {objectPageMeta.totalPages > 1 ? (
              <nav className="public-object-pagination" aria-label="Browse object pages">
                <button type="button" className="public-object-page-arrow" aria-label="Previous object page" onClick={() => changeObjectPage(objectPageMeta.page - 1)} disabled={objectPageMeta.page <= 1 || objectsLoading}>‹</button>
                <span className="public-object-page-numbers">
                  {getPublicObjectPageButtons(objectPageMeta.totalPages, objectPageMeta.page).map((page, index) => page === 'ellipsis' ? (
                    <span key={`ellipsis-${index}`} className="public-object-page-ellipsis" aria-hidden="true">…</span>
                  ) : (
                    <button key={page} type="button" className="public-object-page-number" aria-label={`Page ${page}`} aria-current={page === objectPageMeta.page ? 'page' : undefined} onClick={() => changeObjectPage(page)} disabled={objectsLoading}>{page}</button>
                  ))}
                </span>
                <span className="public-object-page-announcement" aria-live="polite" aria-atomic="true">
                  Page {objectPageMeta.page} of {objectPageMeta.totalPages}{objectsLoading && objects.length ? `; loading page ${objectPage}` : ''}
                </span>
                <button type="button" className="public-object-page-arrow" aria-label="Next object page" onClick={() => changeObjectPage(objectPageMeta.page + 1)} disabled={objectPageMeta.page >= objectPageMeta.totalPages || objectsLoading}>›</button>
              </nav>
            ) : null}
          </div>

          {objectsLoading && objects.length ? (
            <p className="public-object-page-loading" role="status" aria-live="polite">
              Loading page {objectPage}… Page {objectPageMeta.page} objects remain available.
            </p>
          ) : null}

          {loading || (objectsLoading && !objects.length) ? (
            <div
              className="public-browse-loading"
              role="status"
              aria-live="polite"
            >
              <BrandMotion name="scan" size={40} className="brand-motion-glow" />
              <span>Loading published objects…</span>
            </div>
          ) : objectPageError && !objects.length ? (
            <div className="public-object-page-error" role="alert">
              <p>This object page could not be loaded.</p>
              <button type="button" className="ghost" onClick={() => void loadObjectPage(objectPage, activeProjectId)}>Retry</button>
            </div>
          ) : objects.length ? (
            <div key={objectPageMeta.page} className={`public-library-grid public-object-page-grid from-${pageDirection}`} aria-label={`Objects on page ${objectPageMeta.page}`}>
              {objects.map((objectRow, index) => {
                const project = projectById.get(objectRow.project_id)
                const posterLabel = `Open evidence for ${objectRow.name}`
                if (!objectRow.evidence_url) {
                  return (
                    <article key={objectRow.id} className="public-library-card pending motion-browse-entry" style={buildMotionDelayStyle(index, 35, 105)}>
                      <div className="public-library-card-top"><p className="kicker">{project?.name || 'Collection'}</p><span className="public-library-status-chip pending">Listed</span></div>
                      <BrowseCardVisual variant="pending" />
                      <h3>{objectRow.name}</h3>
                      {objectRow.external_url ? <div className="public-library-card-actions"><a className="public-library-inline-link motion-ink-underline" href={objectRow.external_url} target="_blank" rel="noreferrer">Collection website</a></div> : null}
                    </article>
                  )
                }

                return (
                  <article
                    key={objectRow.id}
                    className="public-library-card ready text-led motion-browse-entry"
                    style={buildMotionDelayStyle(index, 40, 120)}
                  >
                    <div className="public-library-card-header">
                      <p className="kicker public-library-card-project-label">{project?.name || 'Collection'}</p>
                    </div>
                    <ReadyCardPosterLink
                      objectRow={objectRow}
                      label={posterLabel}
                      onOpen={openObjectEvidence}
                      posterUrl={resolvePublicObjectPosterUrl(normalizeOptionalText(objectRow.poster_url))}
                    />
                    <h3>{objectRow.name}</h3>
                    <p className="public-library-card-summary">{browseSummary(objectRow)}</p>
                    <div className="public-library-card-actions">
                      <button type="button" className="search-result-button" aria-label={posterLabel} onClick={() => openObjectEvidence(objectRow)}>Open evidence</button>
                      {objectRow.external_url ? (
                        <a className="public-library-inline-link motion-ink-underline" href={objectRow.external_url} target="_blank" rel="noreferrer">
                          Collection website
                        </a>
                      ) : null}
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <ActionEmptyState
              title={activeProjectId ? 'No evidence pages in this collection' : 'No objects open to evidence'}
              text={activeProjectId
                ? 'Try another collection to explore published moments.'
                : 'Search the collection or browse a collection record to continue.'}
              actionLabel={activeProjectId ? 'Browse all collections' : 'Search the collection'}
              onAction={activeProjectId
                ? () => {
                    handleProjectTabChange('')
                    window.requestAnimationFrame(() => {
                      browseSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                    })
                  }
                : () => resetBrowseSurface({ focusSearch: true })}
            />
          )}
          {objectPageError && objects.length ? (
            <div className="public-object-page-error" role="alert">
              <p>These objects could not be loaded. Your current page stays available.</p>
              <button type="button" className="ghost" onClick={() => void loadObjectPage(objectPage, activeProjectId)}>Retry</button>
            </div>
          ) : null}
        </section>
        </div>
      </main>
    </>
  )
}
