function isLocalOnlyHostname(hostname = '') {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1' || hostname === '0.0.0.0'
}

function isAbsoluteUrl(value = '') {
  return /^[a-zA-Z][a-zA-Z\d+.-]*:\/\//.test(value)
}

const defaultApiBase = (() => {
  if (typeof window === 'undefined') {
    return 'http://localhost:8000'
  }

  const { protocol, hostname } = window.location
  return `${protocol}//${hostname}:8000`
})()

function resolveApiBase(rawApiBase) {
  const configuredApiBase = rawApiBase || defaultApiBase

  if (typeof window === 'undefined' || !isAbsoluteUrl(configuredApiBase)) {
    return configuredApiBase
  }

  try {
    const parsedApiBase = new URL(configuredApiBase)
    const runtimeHostname = window.location.hostname

    if (isLocalOnlyHostname(parsedApiBase.hostname) && !isLocalOnlyHostname(runtimeHostname)) {
      parsedApiBase.protocol = window.location.protocol
      parsedApiBase.hostname = runtimeHostname
    }

    return parsedApiBase.toString()
  } catch {
    return configuredApiBase
  }
}

const RAW_API_BASE = resolveApiBase(import.meta.env?.VITE_API_BASE_URL)
const API_BASE = RAW_API_BASE.endsWith('/') ? RAW_API_BASE.slice(0, -1) : RAW_API_BASE
const API_UNAVAILABLE_MESSAGE = `Could not reach the API at ${API_BASE}. Start the backend or set VITE_API_BASE_URL to the correct server.`
const PUBLIC_OBJECT_POSTER_VERSION = 'neutral-20260709'

function createApiError(message, { status = null, kind = '' } = {}) {
  const error = new Error(message)
  if (Number.isFinite(Number(status))) {
    error.status = Number(status)
  }
  if (kind) {
    error.kind = kind
  }
  return error
}

export function resolveApiUrl(path) {
  const normalizedInput = typeof path === 'string' ? path.trim() : ''
  if (!normalizedInput) {
    return ''
  }

  if (isAbsoluteUrl(normalizedInput)) {
    return normalizedInput
  }

  const normalizedPath = normalizedInput.startsWith('/') ? normalizedInput : `/${normalizedInput}`
  if (!API_BASE) {
    return normalizedPath
  }

  if (API_BASE.startsWith('/') && (normalizedPath === API_BASE || normalizedPath.startsWith(`${API_BASE}/`))) {
    return normalizedPath
  }

  return `${API_BASE}${normalizedPath}`
}

function withPublicObjectPosterVersion(path) {
  const normalizedInput = typeof path === 'string' ? path.trim() : ''
  if (!normalizedInput) {
    return ''
  }

  if (!/\/api\/(?:v1\/)?public\/objects\/[^/?#]+\/poster(?:[?#]|$)/i.test(normalizedInput)) {
    return normalizedInput
  }

  const hashIndex = normalizedInput.indexOf('#')
  const pathAndSearch = hashIndex >= 0 ? normalizedInput.slice(0, hashIndex) : normalizedInput
  const hash = hashIndex >= 0 ? normalizedInput.slice(hashIndex) : ''

  if (/(?:[?&])v=/.test(pathAndSearch)) {
    return normalizedInput
  }

  const separator = pathAndSearch.includes('?') ? '&' : '?'
  return `${pathAndSearch}${separator}v=${PUBLIC_OBJECT_POSTER_VERSION}${hash}`
}

export function resolvePublicObjectPosterUrl(path) {
  return resolveApiUrl(withPublicObjectPosterVersion(path))
}

function buildApiUrl(path) {
  return resolveApiUrl(path)
}

function queryString(params = {}) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  const serialized = search.toString()
  return serialized ? `?${serialized}` : ''
}

// Bounded retry/backoff for idempotent public GET reads. Non-GET requests are
// never retried so we never replay a mutation. Backoff is exponential-ish
// (~300ms then ~800ms) unless the server sends a Retry-After hint we honor.
const MAX_GET_RETRIES = 2
const GET_RETRY_BACKOFF_MS = [300, 800]
const MAX_RETRY_AFTER_MS = 5000

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

function retryAfterMs(response) {
  const header = response?.headers?.get?.('Retry-After')
  if (!header) {
    return null
  }
  const seconds = Number.parseInt(header, 10)
  if (!Number.isFinite(seconds) || seconds < 0) {
    return null
  }
  return Math.min(seconds * 1000, MAX_RETRY_AFTER_MS)
}

function backoffForAttempt(attempt) {
  return GET_RETRY_BACKOFF_MS[attempt] ?? GET_RETRY_BACKOFF_MS[GET_RETRY_BACKOFF_MS.length - 1]
}

export async function apiRequest(path, { method = 'GET', token, body } = {}) {
  const normalizedMethod = String(method || 'GET').trim().toUpperCase()
  const headers = {}
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const canRetry = normalizedMethod === 'GET'
  const url = buildApiUrl(path)

  let response
  for (let attempt = 0; ; attempt += 1) {
    try {
      response = await fetch(url, {
        method: normalizedMethod,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined
      })
    } catch (error) {
      if (canRetry && attempt < MAX_GET_RETRIES) {
        await sleep(backoffForAttempt(attempt))
        continue
      }
      throw createApiError(API_UNAVAILABLE_MESSAGE, { kind: 'network' })
    }

    // Only decide on the status here — the body is consumed once we commit to
    // this response, so a retried 429/503 always issues a fresh request.
    if (canRetry && (response.status === 429 || response.status === 503) && attempt < MAX_GET_RETRIES) {
      await sleep(retryAfterMs(response) ?? backoffForAttempt(attempt))
      continue
    }

    break
  }

  const payload = response.status === 204 ? null : await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload?.detail || `Request failed with status ${response.status}`
    const kind = response.status === 401 || response.status === 403
      ? 'unauthorized'
      : response.status === 404
        ? 'not_found'
        : response.status === 429
          ? 'rate_limited'
          : response.status === 503
            ? 'unavailable'
            : ''
    throw createApiError(detail, { status: response.status, kind })
  }

  return payload
}

export async function login(email, password) {
  return apiRequest('/api/v1/auth/login', {
    method: 'POST',
    body: { email, password }
  })
}

export async function getProjects(token) {
  return apiRequest('/api/v1/projects', { token })
}

export async function getPublicProjects() {
  return apiRequest('/api/v1/public/projects')
}

export async function createProject(token, body) {
  return apiRequest('/api/v1/projects', { method: 'POST', token, body })
}

export async function updateProject(token, projectId, body) {
  return apiRequest(`/api/v1/projects/${projectId}`, { method: 'PATCH', token, body })
}

export async function getObjects(token) {
  return apiRequest('/api/v1/objects', { token })
}

export async function getPublicObjects({ projectId } = {}) {
  return apiRequest(`/api/v1/public/objects${queryString({ project_id: projectId })}`)
}

export async function getPublicObjectsPage({ projectId, page = 1, pageSize = 3 } = {}) {
  return apiRequest(`/api/v1/public/objects/page${queryString({ project_id: projectId, page, page_size: pageSize })}`)
}

// Public-collection stats — Contract 2 of public-landing-session-2026-04-23.md.
// Used by the shared apps/web/src/components/PublicStatsPanel rendered on
// both the editorial landing page (/) and the public browse page (/public).
// No auth: the endpoint is unauthenticated and rate-limited via server-side
// 60s caching (apps/api/app/api/v1/endpoints/public.py:get_public_stats).
// Returns { open_now_count: number, in_preparation_count: number }.
export async function fetchPublicStats() {
  return apiRequest('/api/v1/public/stats')
}

export async function createObject(token, body) {
  return apiRequest('/api/v1/objects', { method: 'POST', token, body })
}

export async function updateObject(token, objectId, body) {
  return apiRequest(`/api/v1/objects/${objectId}`, { method: 'PATCH', token, body })
}

export async function getObjectModel(token, objectId) {
  return apiRequest(`/api/v1/objects/${objectId}/model`, { token })
}

export async function getPublicObjectPreview(objectId, { videoId } = {}) {
  return apiRequest(`/api/v1/public/objects/${objectId}/preview${queryString({ video_id: videoId })}`)
}

export async function getEmbedObjectManifest(websiteObjectId) {
  return apiRequest(`/api/public/objects/${encodeURIComponent(websiteObjectId)}`)
}

export async function getPublicEvidencePage(websiteObjectId, { clip, annotation, video, t } = {}) {
  return apiRequest(
    `/api/v1/public/evidence/objects/${encodeURIComponent(websiteObjectId)}${queryString({
      clip_id: clip,
      annotation_id: annotation,
      video_id: video,
      seek_ms: t
    })}`
  )
}

export async function uploadObjectModel(token, objectId, file) {
  return new Promise((resolve, reject) => {
    const formData = new FormData()
    formData.append('model_file', file)

    const request = new XMLHttpRequest()
    request.open('POST', buildApiUrl(`/api/v1/objects/${objectId}/model`))
    request.setRequestHeader('Authorization', `Bearer ${token}`)

    request.onload = () => {
      const payload = (() => {
        try {
          return JSON.parse(request.responseText || '{}')
        } catch {
          return {}
        }
      })()

      if (request.status >= 200 && request.status < 300) {
        resolve(payload)
        return
      }

      const detail = payload?.detail || `Request failed with status ${request.status}`
      reject(new Error(detail))
    }

    request.onerror = () => reject(new Error('Model upload failed due to a network error.'))
    request.send(formData)
  })
}

export async function deleteObjectModel(token, objectId) {
  return apiRequest(`/api/v1/objects/${objectId}/model`, { method: 'DELETE', token })
}

export async function updateObjectModelView(token, objectId, body) {
  return apiRequest(`/api/v1/objects/${objectId}/model`, { method: 'PATCH', token, body })
}

export function getObjectModelFileUrl(token, objectId, cacheKey = '') {
  const suffix = cacheKey ? `&v=${encodeURIComponent(cacheKey)}` : ''
  return `${buildApiUrl(`/api/v1/objects/${objectId}/model/file`)}?access_token=${encodeURIComponent(token)}${suffix}`
}

export async function getObjectModelAnnotations(token, objectId, { publishedOnly = false } = {}) {
  const suffix = publishedOnly ? '?published_only=true' : ''
  return apiRequest(`/api/v1/objects/${objectId}/model/annotations${suffix}`, { token })
}

export async function createObjectModelAnnotation(token, objectId, body) {
  return apiRequest(`/api/v1/objects/${objectId}/model/annotations`, { method: 'POST', token, body })
}

export async function updateObjectModelAnnotation(token, annotationId, body) {
  return apiRequest(`/api/v1/objects/model-annotations/${annotationId}`, { method: 'PATCH', token, body })
}

export async function deleteObjectModelAnnotation(token, annotationId) {
  return apiRequest(`/api/v1/objects/model-annotations/${annotationId}`, { method: 'DELETE', token })
}

export async function markObjectModelAnnotationReviewed(token, annotationId) {
  return apiRequest(`/api/v1/objects/model-annotations/${annotationId}/mark-reviewed`, { method: 'POST', token })
}

export async function getVideos(token) {
  return apiRequest('/api/v1/videos', { token })
}

export async function getPublicVideos({ projectId, objectId } = {}) {
  return apiRequest(`/api/v1/public/videos${queryString({ project_id: projectId, object_id: objectId })}`)
}

export async function updateVideo(token, videoId, body) {
  return apiRequest(`/api/v1/videos/${videoId}`, { method: 'PATCH', token, body })
}

export async function deleteVideo(token, videoId) {
  return apiRequest(`/api/v1/videos/${videoId}`, { method: 'DELETE', token })
}

export async function getMediaFiles(token) {
  return apiRequest('/api/v1/videos/media-files', { token })
}

export async function createVideo(token, body) {
  return apiRequest('/api/v1/videos', { method: 'POST', token, body })
}

export async function uploadMediaFile(token, file, onProgress) {
  return new Promise((resolve, reject) => {
    const formData = new FormData()
    formData.append('media_file', file)

    const request = new XMLHttpRequest()
    request.open('POST', buildApiUrl('/api/v1/videos/media-files'))
    request.setRequestHeader('Authorization', `Bearer ${token}`)

    request.upload.onprogress = (event) => {
      if (!event.lengthComputable || typeof onProgress !== 'function') return
      const percent = Math.min(100, Math.max(0, Math.round((event.loaded / event.total) * 100)))
      onProgress(percent)
    }

    request.onload = () => {
      const payload = (() => {
        try {
          return JSON.parse(request.responseText || '{}')
        } catch {
          return {}
        }
      })()

      if (request.status >= 200 && request.status < 300) {
        if (typeof onProgress === 'function') {
          onProgress(100)
        }
        resolve(payload)
        return
      }

      const detail = payload?.detail || `Request failed with status ${request.status}`
      reject(new Error(detail))
    }

    request.onerror = () => reject(new Error('Upload failed due to a network error.'))
    request.send(formData)
  })
}

export async function ingestTranscript(token, body) {
  return apiRequest('/api/v1/transcripts', { method: 'POST', token, body })
}

export async function getVideoTranscript(token, videoId, { publishedOnly = false } = {}) {
  const suffix = publishedOnly ? '?published_only=true' : ''
  return apiRequest(`/api/v1/transcripts/videos/${videoId}${suffix}`, { token })
}

export async function getTranscripts(token) {
  return apiRequest('/api/v1/transcripts', { token })
}

export async function updateTranscript(token, transcriptId, body) {
  return apiRequest(`/api/v1/transcripts/${transcriptId}`, { method: 'PATCH', token, body })
}

export function getVideoPlaybackUrl(token, videoId) {
  if (!token) {
    throw new Error('Authentication token is required for playback.')
  }
  return `${buildApiUrl(`/api/v1/videos/${videoId}/stream`)}?access_token=${encodeURIComponent(token)}`
}

export async function queueVideoTranscode(token, videoId) {
  return apiRequest(`/api/v1/videos/${videoId}/transcode`, { method: 'POST', token })
}

export async function backfillTranscodes(token, projectId) {
  const suffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return apiRequest(`/api/v1/videos/transcode/backfill${suffix}`, { method: 'POST', token })
}

export async function warmSearchEngine(token) {
  return apiRequest('/api/v1/search/warmup', { method: 'POST', token })
}

export async function searchSegments(token, body) {
  return apiRequest('/api/v1/search/segments', { method: 'POST', token, body })
}

export async function fetchSearchSuggestions(token, { query, projectId, limit = 7, signal } = {}) {
  const params = new URLSearchParams()
  params.set('q', query || '')
  if (projectId) params.set('project_id', projectId)
  if (limit) params.set('limit', String(limit))
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  const endpoint = token ? '/api/v1/search/suggest' : '/api/v1/public/search/suggest'
  const response = await fetch(buildApiUrl(`${endpoint}?${params.toString()}`), {
    method: 'GET',
    headers,
    signal,
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw createApiError(detail?.detail || `Suggest request failed with status ${response.status}`, {
      status: response.status,
    })
  }
  return response.json()
}

export async function rebuildCorpusPhrases(token, projectId) {
  return apiRequest('/api/v1/search/corpus-phrases/rebuild', {
    method: 'POST',
    token,
    body: { project_id: projectId },
  })
}

export async function searchPublicSegments(body) {
  return apiRequest('/api/v1/public/search/segments', { method: 'POST', body })
}

export async function syncLiveDatabase(token) {
  return apiRequest('/api/v1/ops/live-database/sync', { method: 'POST', token })
}

export async function startLiveDatabaseSync(token) {
  return apiRequest('/api/v1/ops/live-database/sync/start', { method: 'POST', token })
}

export async function getLiveDatabaseSyncStatus(token) {
  return apiRequest('/api/v1/ops/live-database/sync/status', { token })
}

export async function submitSearchResultFeedback(token, body) {
  return apiRequest('/api/v1/search/feedback', { method: 'POST', token, body })
}

export async function getSearchTuningSummary(token) {
  return apiRequest('/api/v1/search/tuning/summary', { token })
}

export async function createSearchTuningReport(token) {
  return apiRequest('/api/v1/search/tuning/reports', { method: 'POST', token })
}

export async function approveSearchTuningReport(token, reportId) {
  return apiRequest(`/api/v1/search/tuning/reports/${reportId}/approve-live-comparison`, { method: 'POST', token })
}

export async function createClip(token, body) {
  return apiRequest('/api/v1/clips', { method: 'POST', token, body })
}

export async function deleteClip(token, clipId) {
  return apiRequest(`/api/v1/clips/${clipId}`, { method: 'DELETE', token })
}

export async function listClips(token, { projectId, videoId, status } = {}) {
  const params = new URLSearchParams()
  if (projectId) params.set('project_id', projectId)
  if (videoId) params.set('video_id', videoId)
  if (status) params.set('status', status)
  const query = params.toString()
  return apiRequest(`/api/v1/clips${query ? `?${query}` : ''}`, { token })
}

export function getClipDownloadUrl(token, clipId) {
  return `${buildApiUrl(`/api/v1/clips/${clipId}/download`)}?access_token=${encodeURIComponent(token)}`
}

export function getClipTranscriptUrl(token, clipId) {
  return `${buildApiUrl(`/api/v1/clips/${clipId}/transcript`)}?access_token=${encodeURIComponent(token)}`
}
