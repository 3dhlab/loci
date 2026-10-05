import { useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  backfillTranscodes,
  createSearchTuningReport,
  createObjectModelAnnotation,
  createClip,
  createObject,
  createProject,
  createVideo,
  deleteClip,
  deleteObjectModel,
  deleteObjectModelAnnotation,
  deleteVideo,
  getClipDownloadUrl,
  getClipTranscriptUrl,
  getLiveDatabaseSyncStatus,
  getTranscripts,
  getObjectModel,
  getObjectModelAnnotations,
  getObjectModelFileUrl,
  getPublicObjectPreview,
  getPublicObjects,
  getPublicProjects,
  getPublicVideos,
  getVideoPlaybackUrl,
  getVideoTranscript,
  getMediaFiles,
  getObjects,
  getSearchTuningSummary,
  getProjects,
  resolveApiUrl,
  getVideos,
  ingestTranscript,
  listClips,
  login,
  markObjectModelAnnotationReviewed,
  approveSearchTuningReport,
  queueVideoTranscode,
  searchPublicSegments,
  searchSegments,
  startLiveDatabaseSync,
  submitSearchResultFeedback,
  updateObjectModelView,
  updateObjectModelAnnotation,
  updateObject,
  updateProject,
  updateTranscript,
  updateVideo,
  uploadObjectModel,
  uploadMediaFile
} from './lib/api'
import { navigateToPublicBrowse, navigateToUrl, PUBLIC_BROWSE_PATH, readWindowLocation } from './lib/navigation'
import ModelCanvas from './components/ModelCanvas'
import BrandLockup from './components/BrandLockup'
import ConfirmDialog from './components/ConfirmDialog'
import SearchAutosuggest from './components/SearchAutosuggest'
import PublicExperienceShell from './components/PublicExperienceShell'
import { formatLociTitle } from './lib/seo'
import { StudioThemeToggle, useStudioSurfaceTheme } from './public/studioSurface'

const TOKEN_KEY = 'semantic.console.token'
const SEARCH_PAGE_SIZE = 5
const PUBLIC_PREVIEW_SEARCH_PAGE_SIZE = 5
const PUBLIC_PREVIEW_RESULT_WINDOW = 50
const IS_PUBLIC_APP = import.meta.env.VITE_APP_SURFACE === 'public'

function isLockedPreviewPath(pathname = '') {
  return pathname === '/preview' || pathname.startsWith('/preview/')
}

function readConsoleRouteState() {
  const { pathname, search } = readWindowLocation()
  const params = new URLSearchParams(search || '')

  return {
    previewRouteLocked: !IS_PUBLIC_APP && isLockedPreviewPath(pathname),
    previewProjectId: params.get('projectId') || '',
    previewObjectId: params.get('objectId') || '',
    previewVideoId: params.get('videoId') || ''
  }
}

function defaultSearchRetrievalMode(isPublicPreview = false) {
  return isPublicPreview ? 'transcript_only' : 'combined'
}

function emptySearchForm(retrievalMode = 'transcript_only') {
  return { query: '', project_id: '', video_id: '', retrieval_mode: retrievalMode }
}

const ANALYSIS_SEARCH_FILTERS_STORAGE_KEY = 'loci.analysis.searchFilters'

function readPersistedAnalysisSearchFilters() {
  if (IS_PUBLIC_APP) return null
  if (typeof window === 'undefined' || !window.localStorage) return null
  try {
    const raw = window.localStorage.getItem(ANALYSIS_SEARCH_FILTERS_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}

function buildInitialAnalysisSearchForm() {
  const base = emptySearchForm(defaultSearchRetrievalMode())
  const persisted = readPersistedAnalysisSearchFilters()
  if (!persisted) return base
  return {
    ...base,
    project_id: typeof persisted.project_id === 'string' ? persisted.project_id : base.project_id,
    video_id: typeof persisted.video_id === 'string' ? persisted.video_id : base.video_id,
    retrieval_mode: typeof persisted.retrieval_mode === 'string' && persisted.retrieval_mode
      ? persisted.retrieval_mode
      : base.retrieval_mode
  }
}

function initialSearchMeta(pageSize, resultWindow = 100, retrievalMode = 'transcript_only') {
  return {
    totalResults: 0,
    page: 1,
    pageSize,
    totalPages: 0,
    resultWindow,
    resultWindowCapped: false,
    searchLogId: '',
    retrievalMode
  }
}

function nextStableVideoId() {
  return `video-${Date.now()}-${Math.floor(Math.random() * 1000)}`
}

function normalizeOptionalText(value) {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed || null
}

function readObjectPublicStandfirst(metadata) {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    return ''
  }
  return typeof metadata.public_standfirst === 'string' ? metadata.public_standfirst : ''
}

function buildPublicSurfaceObject(objectRow) {
  if (!objectRow) {
    return null
  }

  const explicitStandfirst = normalizeOptionalText(objectRow.public_standfirst)
  const metadataStandfirst = normalizeOptionalText(readObjectPublicStandfirst(objectRow.metadata_json))
  const fallbackDescription = normalizeOptionalText(objectRow.description)

  return {
    ...objectRow,
    public_standfirst: explicitStandfirst || metadataStandfirst || fallbackDescription
  }
}

function normalizeEvidenceUrl(value) {
  if (typeof value !== 'string') {
    return ''
  }

  const trimmed = value.trim()
  return trimmed || ''
}

function evidenceUrlObjectId(value) {
  const normalized = normalizeEvidenceUrl(value)
  if (!normalized) {
    return ''
  }

  try {
    const url = new URL(normalized, window.location.href)
    const segments = url.pathname.split('/').filter(Boolean)
    if (segments[0] !== 'evidence' || segments[1] !== 'objects') {
      return ''
    }
    return decodeURIComponent(segments[2] || '')
  } catch {
    return ''
  }
}

function evidenceUrlQueryValue(value, key) {
  const normalized = normalizeEvidenceUrl(value)
  if (!normalized || !key) {
    return ''
  }

  try {
    const url = new URL(normalized, window.location.href)
    return url.searchParams.get(key)?.trim() || ''
  } catch {
    return ''
  }
}

function buildEvidenceVideoMomentUrl(baseUrl, stableVideoId, seekMs) {
  const normalizedBaseUrl = normalizeEvidenceUrl(baseUrl)
  const normalizedVideoId = normalizeOptionalText(stableVideoId)
  if (!normalizedBaseUrl || !normalizedVideoId) {
    return ''
  }

  try {
    const url = new URL(normalizedBaseUrl, window.location.href)
    url.search = ''
    url.searchParams.set('video', normalizedVideoId)

    const normalizedSeekMs = Number.isFinite(seekMs) ? Math.max(0, Math.floor(seekMs)) : 0
    if (normalizedSeekMs > 0) {
      url.searchParams.set('t', String(normalizedSeekMs))
    }

    return url.toString()
  } catch {
    return ''
  }
}

function buildSearchResultEvidenceNavigation(result) {
  const targetUrl = normalizeEvidenceUrl(result?.evidence_url)
  const websiteObjectId = normalizeOptionalText(result?.object_public_id) || evidenceUrlObjectId(targetUrl)
  if (!targetUrl || !websiteObjectId) {
    return { targetUrl: '', state: null }
  }

  const stableVideoId = normalizeOptionalText(result?.stable_video_id) || evidenceUrlQueryValue(targetUrl, 'video')
  const seekMs = Number.isFinite(Number(result?.start_ms))
    ? Math.max(0, Math.floor(Number(result.start_ms)))
    : 0

  return {
    targetUrl,
    state: {
      semanticEvidenceHandoff: {
        websiteObjectId,
        annotationId: '',
        videoId: stableVideoId,
        seekMs,
        autoplay: true,
        source: 'public-search'
      }
    }
  }
}

function approximatelySameMoment(leftMs, rightMs, toleranceMs = 400) {
  return Number.isFinite(leftMs)
    && Number.isFinite(rightMs)
    && Math.abs(Number(leftMs) - Number(rightMs)) <= toleranceMs
}

function msWithinWindow(valueMs, startMs, endMs, toleranceMs = 400) {
  if (!Number.isFinite(valueMs) || !Number.isFinite(startMs)) {
    return false
  }

  const windowEnd = Number.isFinite(endMs) ? Number(endMs) : Number(startMs)
  return Number(valueMs) >= Number(startMs) - toleranceMs && Number(valueMs) <= windowEnd + toleranceMs
}

function surroundingContextRange(result) {
  const momentStart = Number.isFinite(Number(result?.start_ms)) ? Math.max(0, Number(result.start_ms)) : 0
  const momentEnd = Number.isFinite(Number(result?.end_ms)) ? Math.max(momentStart, Number(result.end_ms)) : momentStart
  const contextStart = Number.isFinite(Number(result?.context_start_ms))
    ? Math.max(0, Number(result.context_start_ms))
    : momentStart
  const contextEnd = Number.isFinite(Number(result?.context_end_ms))
    ? Math.max(contextStart, Number(result.context_end_ms))
    : momentEnd

  return {
    momentStart,
    momentEnd,
    contextStart,
    contextEnd
  }
}

function segmentWithinResultContext(segment, result) {
  if (!segment || !result) {
    return false
  }

  const { contextStart, contextEnd } = surroundingContextRange(result)
  const segmentStart = Number.isFinite(Number(segment.start_ms)) ? Math.max(0, Number(segment.start_ms)) : 0
  const segmentEnd = Number.isFinite(Number(segment.end_ms)) ? Math.max(segmentStart, Number(segment.end_ms)) : segmentStart

  return segmentEnd >= contextStart - 400 && segmentStart <= contextEnd + 400
}

function buildAuthenticatedAssetUrl(token, assetPath) {
  const normalizedPath = normalizeOptionalText(assetPath)
  if (!normalizedPath) {
    return ''
  }

  const apiUrl = resolveApiUrl(normalizedPath)
  if (!token) {
    return apiUrl
  }

  const separator = apiUrl.includes('?') ? '&' : '?'
  return `${apiUrl}${separator}access_token=${encodeURIComponent(token)}`
}

function searchRetrievalModeLabel(value) {
  if (value === 'visual_only') {
    return 'Visual only'
  }
  if (value === 'combined') {
    return 'Transcript + scene'
  }
  return 'Transcript only'
}

function searchMatchBasisLine(result, retrievalMode = 'transcript_only') {
  if (retrievalMode === 'visual_only') {
    return 'Matched by visual scene, then grounded against the surrounding transcript.'
  }
  if (retrievalMode === 'combined' && result?.visual_score != null) {
    return 'Matched by spoken content and visual scene.'
  }
  return 'Matched by spoken content.'
}

function searchModeSummaryLine(retrievalMode = 'transcript_only') {
  if (retrievalMode === 'visual_only') {
    return 'Scene similarity drives ranking, then the strongest transcript moment inside each matching window is revealed.'
  }
  if (retrievalMode === 'combined') {
    return 'Transcript fit stays primary. Scene similarity only reranks within the matching window so the player flow remains stable.'
  }
  return 'Transcript similarity drives ranking, with the strongest segment revealed inside each matching window.'
}

function fitChipText(label, score) {
  const formatted = formatSemanticFit(score)
  return formatted ? `${label} ${formatted}` : null
}

function shortId(value) {
  if (!value) return 'n/a'
  return `${value.slice(0, 8)}...${value.slice(-4)}`
}

function Card({ title, subtitle, children, className = '', bodyClassName = '' }) {
  return (
    <section className={`card ${className}`.trim()}>
      <header className="card-header">
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </header>
      <div className={`card-body ${bodyClassName}`.trim()}>{children}</div>
    </section>
  )
}

function EmptyState({ title = 'Nothing here yet', text }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p className="muted">{text}</p>
    </div>
  )
}

function FeedbackGlyph({ direction = 'up' }) {
  const transform = direction === 'down' ? 'translate(0 24) scale(1 -1)' : undefined

  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <g transform={transform}>
        <path
          d="M8 10.75h3.15V7.2c0-.98.56-1.86 1.44-2.28l.62-.3c.84-.4 1.8.12 1.97 1.03l.52 5.1h3.2c1.17 0 2.03 1.08 1.78 2.22l-1.08 4.96a1.92 1.92 0 0 1-1.88 1.52H8v-8.7Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.55"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <rect
          x="3.6"
          y="10.75"
          width="2.6"
          height="8.7"
          rx="1"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.55"
        />
      </g>
    </svg>
  )
}

function formatClock(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function formatSemanticFit(score) {
  if (!Number.isFinite(score)) return null
  return `${Math.round(clamp(score, 0, 1) * 100)}/100`
}

function formatPercent(value) {
  if (!Number.isFinite(value)) return '0%'
  return `${Math.round(clamp(value, 0, 1) * 100)}%`
}

function formatDateTime(value) {
  if (!value) return 'Not yet available'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'Not yet available'
  return parsed.toLocaleString()
}

function emptyLiveDatabaseSyncStatus() {
  return {
    operation_id: null,
    state: 'IDLE',
    stage_key: null,
    stage_label: null,
    detail: 'Push every currently published package to the live public site.',
    progress_percent: 0,
    started_at: null,
    finished_at: null,
    generated_at: null,
    project_count: 0,
    object_count: 0,
    video_count: 0,
    transcript_count: 0,
    annotation_count: 0,
    media_file_count: 0,
    error: null
  }
}

function liveDatabaseSyncStateLabel(state) {
  switch (state) {
    case 'RUNNING':
      return 'Running'
    case 'SUCCEEDED':
      return 'Succeeded'
    case 'FAILED':
      return 'Failed'
    default:
      return 'Idle'
  }
}

function preferredTranscriptForVideo(transcriptRows, videoId, { publishedOnly = false } = {}) {
  if (!videoId) {
    return null
  }

  const candidates = transcriptRows.filter((transcript) => {
    if (transcript.video_id !== videoId) {
      return false
    }
    return publishedOnly ? Boolean(transcript.is_published) : true
  })

  if (!candidates.length) {
    return null
  }

  return [...candidates].sort((left, right) => {
    const leftPriority = left.source === 'MANUAL' ? 0 : 1
    const rightPriority = right.source === 'MANUAL' ? 0 : 1
    if (leftPriority !== rightPriority) {
      return leftPriority - rightPriority
    }
    return new Date(right.updated_at) - new Date(left.updated_at)
  })[0]
}

function semanticFitTier(score) {
  if (!Number.isFinite(score)) return null
  const normalized = clamp(score, 0, 1)
  if (normalized >= 0.78) {
    return { label: 'Strong fit', tone: 'strong' }
  }
  if (normalized >= 0.62) {
    return { label: 'Medium fit', tone: 'medium' }
  }
  return { label: 'Loose fit', tone: 'loose' }
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function formatEta(ms) {
  if (!Number.isFinite(ms)) return 'calculating...'
  if (ms <= 0) return '<1 min'
  const seconds = Math.max(0, Math.ceil(ms / 1000))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (minutes <= 0) return `${remainder}s`
  return `${minutes}m ${String(remainder).padStart(2, '0')}s`
}

function shouldShowEta(remainingMs) {
  return Number.isFinite(remainingMs) && remainingMs > 0
}

function activeSegmentIndex(segments, currentMs) {
  if (!segments.length) return -1
  for (let idx = 0; idx < segments.length; idx += 1) {
    const start = Number(segments[idx].start_ms || 0)
    const nextStart = idx + 1 < segments.length ? Number(segments[idx + 1].start_ms || Number.POSITIVE_INFINITY) : Number.POSITIVE_INFINITY
    if (currentMs >= start && currentMs < nextStart) {
      return idx
    }
  }
  return currentMs >= Number(segments[segments.length - 1].start_ms || 0) ? segments.length - 1 : -1
}

function transcodeProgressForVideo(video, nowMs) {
  if (!video || video.status !== 'TRANSCODING') {
    return null
  }
  const percent = clamp(Math.round(Number(video.transcode_progress_pct || 0)), 0, 99)
  const startedAtMs = video.transcode_started_at ? new Date(video.transcode_started_at).getTime() : Number.NaN
  const elapsedMs = Number.isFinite(startedAtMs) ? Math.max(0, nowMs - startedAtMs) : Number.NaN
  const remainingMs = percent >= 12 && percent < 100 && Number.isFinite(elapsedMs) && elapsedMs >= 15000
    ? Math.max(0, Math.round((elapsedMs / percent) * (100 - percent)))
    : Number.NaN
  return { percent, remainingMs, stage: video.transcode_stage || 'queued' }
}

function transcodeStageLabel(stage) {
  switch (stage) {
    case 'queued':
      return 'Queued for worker pickup.'
    case 'preparing':
      return 'Preparing FFmpeg inputs and output target.'
    case 'transcoding':
      return 'Encoding normalized playback asset.'
    case 'finalizing':
      return 'Finalizing output file and publishing playback asset.'
    case 'failed':
      return 'Transcode failed. Retry to regenerate normalized playback output.'
    default:
      return 'Transcoding and normalizing for stable cross-browser playback.'
  }
}

function emptyAnnotationDraft(overrides = {}) {
  return {
    id: '',
    title: '',
    description: '',
    video_id: '',
    clip_id: '',
    transcript_segment_id: '',
    start_ms: '',
    end_ms: '',
    point_x: '',
    point_y: '',
    point_z: '',
    normal_x: '',
    normal_y: '',
    normal_z: '',
    playlist: [],
    ...overrides
  }
}

function normalizeAnnotationPlaylist(annotation) {
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
    video_id: entry.video_id,
    clip_id: entry.clip_id || null,
    transcript_segment_id: entry.transcript_segment_id || null,
    authored_label: entry.label || `Clip ${index + 1}`,
    label: `Clip ${index + 1}`,
    start_ms: Number(entry.start_ms || 0),
    end_ms: Number(entry.end_ms || 0)
  }))
}

function annotationDraftFromAnnotation(annotation) {
  const playlist = normalizeAnnotationPlaylist(annotation)
  const primary = playlist[0] || annotation

  return emptyAnnotationDraft({
    id: annotation.id,
    title: annotation.title,
    description: annotation.description || '',
    video_id: primary.video_id,
    clip_id: primary.clip_id || '',
    transcript_segment_id: primary.transcript_segment_id || '',
    start_ms: String(primary.start_ms),
    end_ms: String(primary.end_ms),
    point_x: String(annotation.point_x),
    point_y: String(annotation.point_y),
    point_z: String(annotation.point_z),
    normal_x: annotation.normal_x == null ? '' : String(annotation.normal_x),
    normal_y: annotation.normal_y == null ? '' : String(annotation.normal_y),
    normal_z: annotation.normal_z == null ? '' : String(annotation.normal_z),
    playlist
  })
}

function cleanImportedTranscriptText(rawText, format) {
  if (!rawText || format === 'PLAIN') {
    return rawText
  }

  return rawText
    .replace(/<\/?font[^>]*>/gi, '')
    .replace(/\{\\[^}]+\}/g, '')
    .replace(/\u00a0/g, ' ')
}

function normalizeModelCameraView(camera = null) {
  const position = Array.isArray(camera?.position) ? camera.position : null
  const target = Array.isArray(camera?.target) ? camera.target : null
  if (!position || !target || position.length !== 3 || target.length !== 3) {
    return null
  }

  const normalizedPosition = position.map((value) => Number(value))
  const normalizedTarget = target.map((value) => Number(value))
  if (normalizedPosition.some((value) => !Number.isFinite(value)) || normalizedTarget.some((value) => !Number.isFinite(value))) {
    return null
  }

  return {
    position: normalizedPosition,
    target: normalizedTarget
  }
}

function cameraViewsEqual(left, right) {
  const normalizedLeft = normalizeModelCameraView(left)
  const normalizedRight = normalizeModelCameraView(right)

  if (!normalizedLeft && !normalizedRight) {
    return true
  }
  if (!normalizedLeft || !normalizedRight) {
    return false
  }

  return normalizedLeft.position.every((value, index) => value === normalizedRight.position[index])
    && normalizedLeft.target.every((value, index) => value === normalizedRight.target[index])
}

function defaultModelTransform(model = null) {
  return {
    position_x: Number(model?.position_x ?? 0),
    position_y: Number(model?.position_y ?? 0),
    position_z: Number(model?.position_z ?? 0),
    rotation_x: Number(model?.rotation_x ?? 0),
    rotation_y: Number(model?.rotation_y ?? 0),
    rotation_z: Number(model?.rotation_z ?? 0)
  }
}

export default function App() {
  const initialRouteState = useMemo(() => readConsoleRouteState(), [])
  const isPreviewRouteLocked = initialRouteState.previewRouteLocked
  const isPrivateAuthoringSurface = !IS_PUBLIC_APP && !isPreviewRouteLocked
  const { palette, toggle: toggleStudioTheme } = useStudioSurfaceTheme({ applyToDocument: isPrivateAuthoringSurface })
  const [token, setToken] = useState(() => (IS_PUBLIC_APP ? '' : localStorage.getItem(TOKEN_KEY) || ''))
  const [mode, setMode] = useState(() => ((IS_PUBLIC_APP || isPreviewRouteLocked) ? 'publicPreview' : 'analysis'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!isPrivateAuthoringSurface) return undefined
    const root = document.documentElement
    root.classList.add('authoring-document-surface')
    return () => {
      root.classList.remove('authoring-document-surface')
    }
  }, [isPrivateAuthoringSurface])

  useEffect(() => {
    if (!isPrivateAuthoringSurface) return undefined
    const root = document.documentElement
    const previousTheme = root.dataset.authoringTheme
    root.dataset.authoringTheme = palette
    return () => {
      if (previousTheme === undefined) delete root.dataset.authoringTheme
      else root.dataset.authoringTheme = previousTheme
    }
  }, [isPrivateAuthoringSurface, palette])

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const [projects, setProjects] = useState([])
  const [objects, setObjects] = useState([])
  const [videos, setVideos] = useState([])
  const [transcripts, setTranscripts] = useState([])
  const [mediaFiles, setMediaFiles] = useState([])
  const [opsObjectModel, setOpsObjectModel] = useState(null)
  const [opsObjectModelFile, setOpsObjectModelFile] = useState(null)
  const [modelProjectId, setModelProjectId] = useState(initialRouteState.previewProjectId || '')
  const [modelObjectId, setModelObjectId] = useState(initialRouteState.previewObjectId || '')
  const [modelDetail, setModelDetail] = useState(null)
  const [modelAnnotations, setModelAnnotations] = useState([])
  const [modelLoading, setModelLoading] = useState(false)
  const [modelUploadFile, setModelUploadFile] = useState(null)
  const [modelBusy, setModelBusy] = useState(false)
  const [modelViewMode, setModelViewMode] = useState(false)
  const [modelTransformDraft, setModelTransformDraft] = useState(defaultModelTransform())
  const [modelCameraDraft, setModelCameraDraft] = useState(null)
  const [annotationFilter, setAnnotationFilter] = useState('ALL')
  const [placementMode, setPlacementMode] = useState(false)
  const [annotationEdit, setAnnotationEdit] = useState(emptyAnnotationDraft())
  const [viewerVideoId, setViewerVideoId] = useState('')
  const [viewerTranscript, setViewerTranscript] = useState(null)
  const [viewerSegments, setViewerSegments] = useState([])
  const [viewerDraftText, setViewerDraftText] = useState('')
  const [viewerHistory, setViewerHistory] = useState({ entries: [], index: -1 })
  const [viewerMediaUrl, setViewerMediaUrl] = useState('')
  const [viewerLoading, setViewerLoading] = useState(false)
  const [viewerCurrentMs, setViewerCurrentMs] = useState(0)
  const [viewerSaving, setViewerSaving] = useState(false)
  const [statusNowMs, setStatusNowMs] = useState(Date.now())

  const [projectForm, setProjectForm] = useState({ name: '', description: '' })
  const [projectEdit, setProjectEdit] = useState({ id: '', name: '', description: '' })
  const [objectEdit, setObjectEdit] = useState({ id: '', name: '', description: '', metadata_json: '' })
  const [objectManagerProjectId, setObjectManagerProjectId] = useState('')
  const [objectForm, setObjectForm] = useState({ project_id: '', name: '', description: '', metadata_json: '' })
  const [videoEdit, setVideoEdit] = useState({ id: '', title: '', object_id: '' })
  const [videoForm, setVideoForm] = useState({
    project_id: '',
    object_id: '',
    stable_video_id: nextStableVideoId(),
    title: '',
    media_path: ''
  })
  const [transcriptEdit, setTranscriptEdit] = useState({ id: '', title: '' })
  const [transcriptForm, setTranscriptForm] = useState({
    video_id: '',
    title: '',
    format: 'SRT',
    language: 'en',
    raw_text:
      '1\\n00:00:00,000 --> 00:00:01,500\\nhello smoke\\n\\n2\\n00:00:01,500 --> 00:00:03,000\\nsecond segment'
  })
  const [lastResult, setLastResult] = useState(null)
  const [mediaUpload, setMediaUpload] = useState(null)
  const [mediaUploading, setMediaUploading] = useState(false)
  const [mediaUploadProgress, setMediaUploadProgress] = useState(0)
  const [analysisSearchForm, setAnalysisSearchForm] = useState(buildInitialAnalysisSearchForm)
  const [analysisSearchLoading, setAnalysisSearchLoading] = useState(false)
  const [analysisSearchResults, setAnalysisSearchResults] = useState([])
  const [analysisSearchMeta, setAnalysisSearchMeta] = useState(() => initialSearchMeta(SEARCH_PAGE_SIZE, 100, defaultSearchRetrievalMode()))
  const [analysisFocusResult, setAnalysisFocusResult] = useState(null)
  const [publicPreviewSearchForm, setPublicPreviewSearchForm] = useState(() => emptySearchForm(defaultSearchRetrievalMode(true)))
  const [publicPreviewSearchLoading, setPublicPreviewSearchLoading] = useState(false)
  const [publicPreviewSearchResults, setPublicPreviewSearchResults] = useState([])
  const [publicPreviewSearchMeta, setPublicPreviewSearchMeta] = useState(
    () => initialSearchMeta(PUBLIC_PREVIEW_SEARCH_PAGE_SIZE, PUBLIC_PREVIEW_RESULT_WINDOW, defaultSearchRetrievalMode(true))
  )
  const [searchFeedbackBySegmentId, setSearchFeedbackBySegmentId] = useState({})
  const [searchFeedbackBusyBySegmentId, setSearchFeedbackBusyBySegmentId] = useState({})
  const [clipBusy, setClipBusy] = useState(false)
  const [recentClips, setRecentClips] = useState([])
  const [manualClip, setManualClip] = useState({ start_ms: '', end_ms: '' })
  const [annotationWorkflowSeed, setAnnotationWorkflowSeed] = useState(null)
  const [pendingAnnotationContext, setPendingAnnotationContext] = useState(null)
  const [annotationMappingPrompt, setAnnotationMappingPrompt] = useState(null)
  const [annotationMappingTargetId, setAnnotationMappingTargetId] = useState('')
  const [selectedModelAnnotation, setSelectedModelAnnotation] = useState(null)
  const [selectedModelAnnotationUrl, setSelectedModelAnnotationUrl] = useState('')
  const [annotationOverlayOpen, setAnnotationOverlayOpen] = useState(false)
  const [annotationOverlayPanel, setAnnotationOverlayPanel] = useState('transcript')
  const [annotationOverlayTranscript, setAnnotationOverlayTranscript] = useState(null)
  const [annotationOverlaySegments, setAnnotationOverlaySegments] = useState([])
  const [annotationOverlayLoading, setAnnotationOverlayLoading] = useState(false)
  const [annotationOverlayError, setAnnotationOverlayError] = useState('')
  const [annotationOverlayCurrentMs, setAnnotationOverlayCurrentMs] = useState(0)
  const [annotationOverlayClipIndex, setAnnotationOverlayClipIndex] = useState(0)
  const [annotationOverlayEnded, setAnnotationOverlayEnded] = useState(false)
  const [pendingPlayback, setPendingPlayback] = useState(null)
  const [publicPreviewData, setPublicPreviewData] = useState(null)
  const [publicPreviewLoading, setPublicPreviewLoading] = useState(false)
  const [publicPreviewVideoId, setPublicPreviewVideoId] = useState(initialRouteState.previewVideoId || '')
  const [publicPreviewPendingPlayback, setPublicPreviewPendingPlayback] = useState(null)
  const [publicPreviewPendingClipCommit, setPublicPreviewPendingClipCommit] = useState(null)
  const [publicPreviewCurrentMs, setPublicPreviewCurrentMs] = useState(0)
  const [publicPreviewAnnotationId, setPublicPreviewAnnotationId] = useState('')
  const [publicPreviewClipIndex, setPublicPreviewClipIndex] = useState(0)
  const [publicPreviewClipPlayback, setPublicPreviewClipPlayback] = useState(null)
  const [publicPreviewSearchMoment, setPublicPreviewSearchMoment] = useState(null)
  const [publicPreviewShouldScrollViewerIntoView, setPublicPreviewShouldScrollViewerIntoView] = useState(false)
  const [modelPublicationVideoId, setModelPublicationVideoId] = useState('')
  const [publicationBusy, setPublicationBusy] = useState(false)
  const [liveDatabaseSyncStatus, setLiveDatabaseSyncStatus] = useState(() => emptyLiveDatabaseSyncStatus())
  const [liveDatabaseSyncRequestBusy, setLiveDatabaseSyncRequestBusy] = useState(false)
  const [tuningSummary, setTuningSummary] = useState(null)
  const [tuningLoading, setTuningLoading] = useState(false)
  const [tuningBusy, setTuningBusy] = useState(false)
  const [confirmAction, setConfirmAction] = useState(null)

  const isPublicPreviewMode = mode === 'publicPreview'
  const publicSurfaceTitle = IS_PUBLIC_APP ? 'Live Database' : 'Public Preview'
  const searchForm = isPublicPreviewMode ? publicPreviewSearchForm : analysisSearchForm
  const setSearchForm = isPublicPreviewMode ? setPublicPreviewSearchForm : setAnalysisSearchForm
  const searchLoading = isPublicPreviewMode ? publicPreviewSearchLoading : analysisSearchLoading
  const setSearchLoading = isPublicPreviewMode ? setPublicPreviewSearchLoading : setAnalysisSearchLoading
  const searchResults = isPublicPreviewMode ? publicPreviewSearchResults : analysisSearchResults
  const setSearchResults = isPublicPreviewMode ? setPublicPreviewSearchResults : setAnalysisSearchResults
  const searchMeta = isPublicPreviewMode ? publicPreviewSearchMeta : analysisSearchMeta
  const setSearchMeta = isPublicPreviewMode ? setPublicPreviewSearchMeta : setAnalysisSearchMeta

  const sortedProjects = useMemo(() => {
    return [...projects].sort((a, b) => a.name.localeCompare(b.name))
  }, [projects])
  const sortedObjects = useMemo(() => {
    return [...objects].sort((a, b) => a.name.localeCompare(b.name))
  }, [objects])
  const sortedVideos = useMemo(() => {
    return [...videos].sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
  }, [videos])
  const publishedObjects = useMemo(() => {
    if (IS_PUBLIC_APP) {
      return sortedObjects
    }
    return sortedObjects.filter((objectRow) => objectRow.is_published)
  }, [sortedObjects])
  const publishedObjectIds = useMemo(() => new Set(publishedObjects.map((objectRow) => objectRow.id)), [publishedObjects])
  const publishedVideos = useMemo(() => {
    if (IS_PUBLIC_APP) {
      return sortedVideos.filter((video) => video.object_id && publishedObjectIds.has(video.object_id))
    }
    return sortedVideos.filter((video) => video.is_published && video.object_id && publishedObjectIds.has(video.object_id))
  }, [publishedObjectIds, sortedVideos])
  const publishedProjects = useMemo(() => {
    const publishedProjectIds = new Set(publishedObjects.map((objectRow) => objectRow.project_id))
    return sortedProjects.filter((project) => publishedProjectIds.has(project.id))
  }, [publishedObjects, sortedProjects])
  const filteredObjectsForManager = useMemo(() => {
    return sortedObjects.filter((objectRow) => !objectManagerProjectId || objectRow.project_id === objectManagerProjectId)
  }, [objectManagerProjectId, sortedObjects])
  const filteredObjectsForModelTab = useMemo(() => {
    return sortedObjects.filter((objectRow) => !modelProjectId || objectRow.project_id === modelProjectId)
  }, [modelProjectId, sortedObjects])
  const filteredPublishedObjectsForPreview = useMemo(() => {
    return publishedObjects.filter((objectRow) => !modelProjectId || objectRow.project_id === modelProjectId)
  }, [modelProjectId, publishedObjects])
  const openNowObjectIds = useMemo(() => {
    if (IS_PUBLIC_APP) {
      return new Set(publishedObjects.map((objectRow) => objectRow.id))
    }
    const readyVideoObjectIds = new Set()
    const videoIdToObjectId = new Map()
    sortedVideos.forEach((video) => {
      if (!video.object_id || !publishedObjectIds.has(video.object_id)) return
      if (video.is_published && video.status === 'READY') {
        readyVideoObjectIds.add(video.object_id)
        videoIdToObjectId.set(video.id, video.object_id)
      }
    })
    const publishedTranscriptObjectIds = new Set()
    transcripts.forEach((transcript) => {
      if (!transcript.is_published) return
      const objectId = videoIdToObjectId.get(transcript.video_id)
      if (objectId) {
        publishedTranscriptObjectIds.add(objectId)
      }
    })
    const result = new Set()
    publishedObjects.forEach((objectRow) => {
      if (readyVideoObjectIds.has(objectRow.id) && publishedTranscriptObjectIds.has(objectRow.id)) {
        result.add(objectRow.id)
      }
    })
    return result
  }, [publishedObjects, publishedObjectIds, sortedVideos, transcripts])
  const selectedObjectDetails = useMemo(() => {
    return sortedObjects.find((objectRow) => objectRow.id === objectEdit.id) || null
  }, [objectEdit.id, sortedObjects])
  const activeModelObject = useMemo(() => {
    return sortedObjects.find((objectRow) => objectRow.id === modelObjectId) || null
  }, [modelObjectId, sortedObjects])
  const filteredVideosForModelTab = useMemo(() => {
    if (activeModelObject) {
      return sortedVideos.filter((video) => video.project_id === activeModelObject.project_id)
    }
    return sortedVideos.filter((video) => !modelProjectId || video.project_id === modelProjectId)
  }, [activeModelObject, modelProjectId, sortedVideos])
  const publicationVideosForModelTab = useMemo(() => {
    if (!activeModelObject) {
      return filteredVideosForModelTab
    }

    const annotationVideoIds = new Set(modelAnnotations.map((annotation) => annotation.video_id))
    const evidenceFirst = filteredVideosForModelTab.filter((video) => {
      return video.object_id === activeModelObject.id || annotationVideoIds.has(video.id)
    })

    return evidenceFirst.length ? evidenceFirst : filteredVideosForModelTab
  }, [activeModelObject, filteredVideosForModelTab, modelAnnotations])
  const filteredModelAnnotations = useMemo(() => {
    if (annotationFilter === 'ALL') {
      return modelAnnotations
    }
    return modelAnnotations.filter((annotation) => annotation.review_status === annotationFilter)
  }, [annotationFilter, modelAnnotations])
  const modelReviewRequiredCount = useMemo(() => {
    return modelAnnotations.filter((annotation) => annotation.review_status === 'REVIEW_REQUIRED').length
  }, [modelAnnotations])
  const savedModelTransform = useMemo(() => ({
    position_x: Number(modelDetail?.position_x ?? 0),
    position_y: Number(modelDetail?.position_y ?? 0),
    position_z: Number(modelDetail?.position_z ?? 0),
    rotation_x: Number(modelDetail?.rotation_x ?? 0),
    rotation_y: Number(modelDetail?.rotation_y ?? 0),
    rotation_z: Number(modelDetail?.rotation_z ?? 0)
  }), [modelDetail])
  const savedModelCameraView = useMemo(() => normalizeModelCameraView(modelDetail?.default_camera_json), [modelDetail])
  const currentModelTransform = useMemo(() => {
    return modelViewMode ? modelTransformDraft : savedModelTransform
  }, [modelTransformDraft, modelViewMode, savedModelTransform])
  const currentModelCameraView = useMemo(() => {
    return modelViewMode ? normalizeModelCameraView(modelCameraDraft) : savedModelCameraView
  }, [modelCameraDraft, modelViewMode, savedModelCameraView])
  const modelFileUrl = useMemo(() => {
    if (!token || !modelDetail || !modelObjectId || modelDetail.object_id !== modelObjectId) {
      return ''
    }
    return getObjectModelFileUrl(token, modelDetail.object_id, `${modelDetail.revision_number}-${modelDetail.updated_at}`)
  }, [token, modelDetail, modelObjectId])
  const selectedModelAnnotationVideo = useMemo(() => {
    return selectedModelAnnotation ? sortedVideos.find((video) => video.id === selectedModelAnnotation.video_id) || null : null
  }, [selectedModelAnnotation, sortedVideos])
  const selectedModelAnnotationPlaylist = useMemo(() => {
    return normalizeAnnotationPlaylist(selectedModelAnnotation)
  }, [selectedModelAnnotation])
  const annotationMappingPromptVideo = useMemo(() => {
    return annotationMappingPrompt ? sortedVideos.find((video) => video.id === annotationMappingPrompt.videoId) || null : null
  }, [annotationMappingPrompt, sortedVideos])
  const annotationOverlayClip = useMemo(() => {
    return selectedModelAnnotationPlaylist[annotationOverlayClipIndex] || null
  }, [selectedModelAnnotationPlaylist, annotationOverlayClipIndex])
  const annotationOverlayClipVideo = useMemo(() => {
    return annotationOverlayClip ? sortedVideos.find((video) => video.id === annotationOverlayClip.video_id) || null : null
  }, [annotationOverlayClip, sortedVideos])
  const annotationOverlaySegmentIndex = useMemo(() => {
    return activeSegmentIndex(annotationOverlaySegments, annotationOverlayCurrentMs)
  }, [annotationOverlaySegments, annotationOverlayCurrentMs])
  const activeVideo = useMemo(() => sortedVideos.find((video) => video.id === viewerVideoId) || null, [sortedVideos, viewerVideoId])
  const viewerReady = activeVideo?.status === 'READY'
  const viewerBlocked = Boolean(activeVideo) && !viewerReady
  const transcodeMeta = useMemo(() => {
    if (!activeVideo) {
      return { show: false, percent: 0, remainingMs: Number.NaN, label: '' }
    }
    if (activeVideo.status === 'READY') {
      return { show: false, percent: 100, remainingMs: 0, label: '' }
    }
    if (activeVideo.status === 'FAILED') {
      return {
        show: true,
        percent: 100,
        remainingMs: 0,
        label: transcodeStageLabel('failed')
      }
    }
    const progress = transcodeProgressForVideo(activeVideo, statusNowMs)

    return {
      show: true,
      percent: progress?.percent || 0,
      remainingMs: progress?.remainingMs ?? Number.NaN,
      label: transcodeStageLabel(progress?.stage)
    }
  }, [activeVideo, statusNowMs])
  const currentSegmentIndex = useMemo(() => activeSegmentIndex(viewerSegments, viewerCurrentMs), [viewerSegments, viewerCurrentMs])
  const analysisFocusContext = useMemo(() => {
    if (!analysisFocusResult || analysisFocusResult.video_id !== viewerVideoId) {
      return null
    }

    return {
      ...surroundingContextRange(analysisFocusResult),
      retrievalMode: analysisSearchMeta.retrievalMode || defaultSearchRetrievalMode()
    }
  }, [analysisFocusResult, analysisSearchMeta.retrievalMode, viewerVideoId])
  const analysisFocusMatchLine = useMemo(() => {
    if (!analysisFocusResult) {
      return ''
    }
    return searchMatchBasisLine(analysisFocusResult, analysisSearchMeta.retrievalMode)
  }, [analysisFocusResult, analysisSearchMeta.retrievalMode])
  const analysisFocusThumbnailUrl = useMemo(() => {
    return buildAuthenticatedAssetUrl(token, analysisFocusResult?.thumbnail_url)
  }, [analysisFocusResult?.thumbnail_url, token])
  const analysisFocusSampleFrames = useMemo(() => {
    return (analysisFocusResult?.sample_frames || [])
      .map((frame) => ({
        ...frame,
        resolved_asset_url: buildAuthenticatedAssetUrl(token, frame.asset_url)
      }))
      .filter((frame) => frame.resolved_asset_url)
  }, [analysisFocusResult?.sample_frames, token])
  const analysisFocusContextSegments = useMemo(() => {
    if (!analysisFocusResult || analysisFocusResult.video_id !== viewerVideoId) {
      return []
    }
    return viewerSegments.filter((segment) => segmentWithinResultContext(segment, analysisFocusResult))
  }, [analysisFocusResult, viewerSegments, viewerVideoId])
  const publicPreviewVideo = publicPreviewData?.video || null
  const publicPreviewSegments = publicPreviewData?.segments || []
  const publicPreviewSelectedAnnotation = useMemo(() => {
    return publicPreviewData?.annotations?.find((annotation) => annotation.id === publicPreviewAnnotationId) || null
  }, [publicPreviewAnnotationId, publicPreviewData])
  const publicPreviewSelectedAnnotationPlaylist = useMemo(() => {
    return normalizeAnnotationPlaylist(publicPreviewSelectedAnnotation)
  }, [publicPreviewSelectedAnnotation])
  const publicPreviewClip = useMemo(() => {
    return publicPreviewSelectedAnnotationPlaylist[publicPreviewClipIndex] || null
  }, [publicPreviewClipIndex, publicPreviewSelectedAnnotationPlaylist])
  const publicPreviewClipVideo = useMemo(() => {
    return publicPreviewClip ? sortedVideos.find((video) => video.id === publicPreviewClip.video_id) || null : null
  }, [publicPreviewClip, sortedVideos])
  const publicPreviewObjectEvidenceUrl = useMemo(
    () => normalizeEvidenceUrl(publicPreviewData?.object?.evidence_url),
    [publicPreviewData?.object?.evidence_url]
  )
  const publicPreviewCurrentStableVideoId = useMemo(
    () => publicPreviewClipVideo?.stable_video_id || publicPreviewVideo?.stable_video_id || '',
    [publicPreviewClipVideo?.stable_video_id, publicPreviewVideo?.stable_video_id]
  )
  const publicPreviewCurrentSegmentIndex = useMemo(() => {
    return activeSegmentIndex(publicPreviewSegments, publicPreviewCurrentMs)
  }, [publicPreviewSegments, publicPreviewCurrentMs])
  const publicPreviewModelTransform = useMemo(() => defaultModelTransform(publicPreviewData?.model), [publicPreviewData?.model])
  const publicPreviewCameraView = useMemo(() => {
    return normalizeModelCameraView(publicPreviewSelectedAnnotation?.camera_json)
      || normalizeModelCameraView(publicPreviewData?.model?.default_camera_json)
  }, [publicPreviewData?.model?.default_camera_json, publicPreviewSelectedAnnotation?.camera_json])
  const currentModelPublicationVideo = useMemo(() => {
    if (!publicationVideosForModelTab.length) {
      return null
    }
    return publicationVideosForModelTab.find((video) => video.id === modelPublicationVideoId) || publicationVideosForModelTab[0]
  }, [modelPublicationVideoId, publicationVideosForModelTab])
  const currentModelPublicationTranscript = useMemo(() => {
    return preferredTranscriptForVideo(transcripts, currentModelPublicationVideo?.id || '')
  }, [currentModelPublicationVideo?.id, transcripts])
  const publishedModelAnnotationCount = useMemo(() => {
    return modelAnnotations.filter((annotation) => annotation.is_published).length
  }, [modelAnnotations])
  const publicationPackagePublished = useMemo(() => {
    const annotationsPublished = modelAnnotations.every((annotation) => annotation.is_published)
    return Boolean(
      activeModelObject?.is_published
      && (!modelDetail || modelDetail.is_published)
      && (!currentModelPublicationVideo || currentModelPublicationVideo.is_published)
      && (!currentModelPublicationTranscript || currentModelPublicationTranscript.is_published)
      && annotationsPublished
    )
  }, [
    activeModelObject?.is_published,
    currentModelPublicationTranscript?.is_published,
    currentModelPublicationVideo?.is_published,
    modelAnnotations,
    modelDetail?.is_published
  ])
  const publicationPackageHasPublishedContent = Boolean(
    activeModelObject?.is_published
    || modelDetail?.is_published
    || currentModelPublicationVideo?.is_published
    || currentModelPublicationTranscript?.is_published
    || publishedModelAnnotationCount > 0
  )
  const publicationBlockers = useMemo(() => {
    if (!activeModelObject) return []
    const blockers = []
    if (!activeModelObject.is_published) {
      blockers.push({ key: 'object-private', label: 'Object is private' })
    }
    if (!modelDetail) {
      blockers.push({ key: 'no-model', label: '3D model is not uploaded' })
    } else if (!modelDetail.is_published) {
      blockers.push({ key: 'model-private', label: '3D model is private' })
    }
    if (!currentModelPublicationVideo) {
      blockers.push({ key: 'no-video', label: 'Video is not selected' })
    } else {
      if (!currentModelPublicationVideo.is_published) {
        blockers.push({ key: 'video-private', label: 'Video is private' })
      }
      if (currentModelPublicationVideo.status !== 'READY') {
        blockers.push({ key: 'video-not-ready', label: 'Video is not READY' })
      }
    }
    if (!currentModelPublicationTranscript) {
      blockers.push({ key: 'no-transcript', label: 'Transcript is not ingested' })
    } else if (!currentModelPublicationTranscript.is_published) {
      blockers.push({ key: 'transcript-private', label: 'Transcript is private' })
    }
    const unpublishedAnnotationCount = modelAnnotations.length - publishedModelAnnotationCount
    if (modelAnnotations.length > 0 && unpublishedAnnotationCount > 0) {
      blockers.push({
        key: 'annotations-unpublished',
        label: `${unpublishedAnnotationCount} of ${modelAnnotations.length} annotations are not published`
      })
    }
    return blockers
  }, [
    activeModelObject,
    modelDetail,
    currentModelPublicationVideo,
    currentModelPublicationTranscript,
    modelAnnotations,
    publishedModelAnnotationCount
  ])
  const packageOpenNow = Boolean(activeModelObject) && publicationBlockers.length === 0
  const packageStatusLabel = packageOpenNow
    ? 'Open now'
    : publicationPackagePublished
      ? 'Published · incomplete'
      : 'Private'
  const liveDatabaseSyncBusy = liveDatabaseSyncRequestBusy || liveDatabaseSyncStatus.state === 'RUNNING'
  const liveDatabaseSyncProgress = Math.max(0, Math.min(100, Number(liveDatabaseSyncStatus.progress_percent || 0)))
  const liveDatabaseSyncCountsAvailable = [
    liveDatabaseSyncStatus.object_count,
    liveDatabaseSyncStatus.video_count,
    liveDatabaseSyncStatus.annotation_count,
    liveDatabaseSyncStatus.media_file_count
  ].some((value) => Number(value || 0) > 0)
  const tuningLiveWindow = tuningSummary?.live_window || null
  const tuningLatestReport = tuningSummary?.latest_report || null
  const selectedMedia = useMemo(
    () => mediaFiles.find((item) => item.path === videoForm.media_path) || null,
    [mediaFiles, videoForm.media_path]
  )
  const videoRef = useRef(null)
  const publicPreviewVideoRef = useRef(null)
  const transcriptListRef = useRef(null)
  const publicPreviewTranscriptListRef = useRef(null)
  const publicPreviewViewerRef = useRef(null)
  const annotationOverlayTranscriptListRef = useRef(null)
  const clipExportAnchorRef = useRef(null)
  const analysisViewerRef = useRef(null)
  const modelEditorRef = useRef(null)
  const annotationOverlayVideoRef = useRef(null)
  const modelWorkspaceLoadIdRef = useRef(0)
  const publicPreviewLoadIdRef = useRef(0)
  const opsModelFileInputRef = useRef(null)
  const modelFileInputRef = useRef(null)
  const previousModeRef = useRef(mode)
  const analysisSessionRef = useRef(null)
  const viewerDraftSourceRef = useRef(null)
  const previousLiveDatabaseSyncStateRef = useRef(liveDatabaseSyncStatus.state)

  function initializeViewerHistory(nextText) {
    setViewerHistory({ entries: nextText ? [nextText] : [], index: nextText ? 0 : -1 })
  }

  function pushUndoState(nextText) {
    setViewerHistory((prev) => {
      if (prev.entries[prev.index] === nextText) {
        return prev
      }

      const nextEntries = [...prev.entries.slice(0, prev.index + 1), nextText]
      const trimmedEntries = nextEntries.length > 200 ? nextEntries.slice(nextEntries.length - 200) : nextEntries
      return {
        entries: trimmedEntries,
        index: trimmedEntries.length - 1
      }
    })
  }

  function applyPendingPlayback(media) {
    if (!media || !pendingPlayback || pendingPlayback.videoId !== viewerVideoId) {
      return false
    }
    if (media.readyState < 1) {
      return false
    }

    const target = Math.max(0, Math.floor(pendingPlayback.seekMs || 0))
    media.currentTime = target / 1000
    setViewerCurrentMs(target)

    const shouldAutoplay = pendingPlayback.autoplay
    setPendingPlayback(null)

    if (shouldAutoplay) {
      media.play().catch(() => null)
    } else {
      media.pause()
    }

    return true
  }

  function applyPublicPreviewPendingPlayback(media) {
    if (!media || !publicPreviewPendingPlayback || publicPreviewPendingPlayback.videoId !== publicPreviewVideo?.id) {
      return false
    }
    if (media.readyState < 1) {
      return false
    }

    const target = Math.max(0, Math.floor(publicPreviewPendingPlayback.seekMs || 0))
    const durationMs = Number.isFinite(media.duration) ? Math.floor(media.duration * 1000) : null
    const clampedMs = durationMs && durationMs > 250 ? Math.min(target, durationMs - 250) : target

    const shouldAutoplay = Boolean(publicPreviewPendingPlayback.autoplay)
    const pendingClipCommit = publicPreviewPendingClipCommit && publicPreviewPendingClipCommit.videoId === publicPreviewVideo?.id
      ? publicPreviewPendingClipCommit
      : null

    flushSync(() => {
      setPublicPreviewCurrentMs(clampedMs)
      if (pendingClipCommit) {
        setPublicPreviewAnnotationId(pendingClipCommit.annotationId)
        setPublicPreviewClipIndex(pendingClipCommit.clipIndex)
        setPublicPreviewClipPlayback(pendingClipCommit.clipPlayback)
        setPublicPreviewPendingClipCommit(null)
      }
      setPublicPreviewPendingPlayback(null)
    })

    media.currentTime = clampedMs / 1000

    if (shouldAutoplay) {
      media.play().catch(() => null)
    } else {
      media.pause()
    }

    return true
  }

  async function refreshData(activeToken = token) {
    if (IS_PUBLIC_APP) {
      const [projectRows, objectRows, videoRows] = await Promise.all([
        getPublicProjects(),
        getPublicObjects(),
        getPublicVideos()
      ])
      setProjects(projectRows)
      setObjects(objectRows)
      setVideos(videoRows)
      setTranscripts([])
      setMediaFiles([])
      return
    }

    if (!activeToken) return
    const [projectRows, objectRows, videoRows, transcriptRows, mediaRows] = await Promise.all([
      getProjects(activeToken),
      getObjects(activeToken),
      getVideos(activeToken),
      getTranscripts(activeToken),
      getMediaFiles(activeToken)
    ])
    setProjects(projectRows)
    setObjects(objectRows)
    setVideos(videoRows)
    setTranscripts(transcriptRows)
    setMediaFiles(mediaRows)

    if (!transcriptForm.video_id && videoRows.length) {
      setTranscriptForm((prev) => ({
        ...prev,
        video_id: videoRows[0].id,
        title: prev.title || `${videoRows[0].title} Transcript`
      }))
    }
    if (!objectForm.project_id && projectRows.length) {
      setObjectForm((prev) => ({ ...prev, project_id: projectRows[0].id }))
    }
    if (!videoForm.project_id && projectRows.length) {
      setVideoForm((prev) => ({ ...prev, project_id: projectRows[0].id }))
    }
    if (!videoForm.media_path && mediaRows.length) {
      setVideoForm((prev) => ({ ...prev, media_path: mediaRows[0].path }))
    }
  }

  async function refreshClips(activeToken = token) {
    if (!activeToken) return
    try {
      const payload = await listClips(activeToken, {
        projectId: searchForm.project_id || activeVideo?.project_id || undefined
      })
      setRecentClips((payload.clips || []).slice(0, 12))
    } catch {
      setRecentClips([])
    }
  }

  async function loadTuningSummary(activeToken = token) {
    if (!activeToken) return null
    setTuningLoading(true)
    try {
      const payload = await getSearchTuningSummary(activeToken)
      setTuningSummary(payload)
      return payload
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setTuningLoading(false)
    }
  }

  useEffect(() => {
    if (IS_PUBLIC_APP) {
      refreshData().catch((err) => setError(err.message))
      return
    }

    if (!token) return
    refreshData()
      .then(() => refreshClips(token))
      .catch((err) => setError(err.message))
  }, [token])

  useEffect(() => {
    if (IS_PUBLIC_APP) return
    if (typeof window === 'undefined' || !window.localStorage) return
    try {
      const payload = {
        project_id: analysisSearchForm.project_id || '',
        video_id: analysisSearchForm.video_id || '',
        retrieval_mode: analysisSearchForm.retrieval_mode || defaultSearchRetrievalMode()
      }
      window.localStorage.setItem(ANALYSIS_SEARCH_FILTERS_STORAGE_KEY, JSON.stringify(payload))
    } catch {
      // ignore storage errors (private mode, quota exceeded)
    }
  }, [analysisSearchForm.project_id, analysisSearchForm.video_id, analysisSearchForm.retrieval_mode])

  useEffect(() => {
    if (IS_PUBLIC_APP) {
      return
    }

    if (!token || (mode !== 'analysis' && mode !== 'model3d' && mode !== 'publicPreview')) return
    refreshData(token)
      .then(() => refreshClips(token))
      .catch(() => null)
  }, [mode, token])

  useEffect(() => {
    if (!token || mode !== 'tuning') {
      return
    }

    loadTuningSummary(token).catch(() => null)
  }, [mode, token])

  useEffect(() => {
    if (!isPreviewRouteLocked || mode === 'publicPreview') {
      return
    }

    setMode('publicPreview')
  }, [isPreviewRouteLocked, mode])

  useEffect(() => {
    if (modelProjectId) {
      return
    }

    const defaultProjectId = mode === 'publicPreview'
      ? (publishedProjects[0]?.id || sortedProjects[0]?.id)
      : sortedProjects[0]?.id

    if (defaultProjectId) {
      setModelProjectId(defaultProjectId)
    }
  }, [mode, modelProjectId, publishedProjects, sortedProjects])

  useEffect(() => {
    if (mode !== 'publicPreview' || modelObjectId || modelProjectId || !publishedObjects.length) {
      return
    }

    setModelProjectId(publishedObjects[0].project_id)
    setModelObjectId(publishedObjects[0].id)
  }, [mode, modelObjectId, modelProjectId, publishedObjects])

  useEffect(() => {
    if (mode === 'publicPreview') {
      if (!filteredPublishedObjectsForPreview.length) {
        if (modelObjectId) {
          setModelObjectId('')
        }
        return
      }

      if (!modelObjectId || !filteredPublishedObjectsForPreview.some((objectRow) => objectRow.id === modelObjectId)) {
        setModelObjectId(filteredPublishedObjectsForPreview[0].id)
      }
      return
    }

    if (!filteredObjectsForModelTab.length) {
      if (modelObjectId) {
        setModelObjectId('')
      }
      return
    }

    if (!modelObjectId || !filteredObjectsForModelTab.some((objectRow) => objectRow.id === modelObjectId)) {
      setModelObjectId(filteredObjectsForModelTab[0].id)
    }
  }, [mode, filteredObjectsForModelTab, filteredPublishedObjectsForPreview, modelObjectId])

  useEffect(() => {
    if (!publicationVideosForModelTab.length) {
      if (modelPublicationVideoId) {
        setModelPublicationVideoId('')
      }
      return
    }

    if (!modelPublicationVideoId || !publicationVideosForModelTab.some((video) => video.id === modelPublicationVideoId)) {
      setModelPublicationVideoId(publicationVideosForModelTab[0].id)
    }
  }, [modelPublicationVideoId, publicationVideosForModelTab])

  useEffect(() => {
    if (mode !== 'model3d' || !selectedObjectDetails) {
      return
    }

    if (modelProjectId !== selectedObjectDetails.project_id) {
      setModelProjectId(selectedObjectDetails.project_id)
    }
    if (modelObjectId !== selectedObjectDetails.id) {
      setModelObjectId(selectedObjectDetails.id)
    }
  }, [mode, modelObjectId, modelProjectId, selectedObjectDetails])

  useEffect(() => {
    setPlacementMode(false)
    setSelectedModelAnnotation(null)
    setAnnotationOverlayOpen(false)
    setAnnotationOverlayClipIndex(0)
    setAnnotationEdit((prev) => {
      if (pendingAnnotationContext && pendingAnnotationContext.objectId === modelObjectId) {
        return prev
      }
      return emptyAnnotationDraft({
        ...prev,
        video_id: prev.video_id && filteredVideosForModelTab.some((video) => video.id === prev.video_id)
          ? prev.video_id
          : (filteredVideosForModelTab[0]?.id || '')
      })
    })
  }, [modelObjectId, filteredVideosForModelTab])

  useEffect(() => {
    if (!annotationMappingPrompt) {
      return
    }

    if (annotationMappingPrompt.objectId !== modelObjectId) {
      setAnnotationMappingPrompt(null)
      setAnnotationMappingTargetId('')
    }
  }, [annotationMappingPrompt, modelObjectId])

  useEffect(() => {
    if (!pendingAnnotationContext || mode !== 'model3d') {
      return
    }

    if (modelProjectId !== pendingAnnotationContext.projectId) {
      setModelProjectId(pendingAnnotationContext.projectId)
      return
    }

    if (modelObjectId !== pendingAnnotationContext.objectId) {
      setModelObjectId(pendingAnnotationContext.objectId)
      return
    }

    setAnnotationMappingPrompt({ ...pendingAnnotationContext })
    setAnnotationMappingTargetId((current) => current || modelAnnotations[0]?.id || '')
    setSelectedModelAnnotation(null)
    setAnnotationEdit(
      emptyAnnotationDraft({
        title: pendingAnnotationContext.title,
        description: pendingAnnotationContext.description,
        video_id: pendingAnnotationContext.videoId,
        start_ms: String(pendingAnnotationContext.startMs),
        end_ms: String(pendingAnnotationContext.endMs),
        playlist: [
          buildAnnotationSequenceEntry(
            pendingAnnotationContext.videoId,
            pendingAnnotationContext.startMs,
            pendingAnnotationContext.endMs,
            0
          )
        ]
      })
    )
    setPlacementMode(false)
    setPendingAnnotationContext(null)
    setNotice(
      modelAnnotations.length
        ? '3D mapping context loaded. Review the mapped range in the editor, then choose create new or add to an existing annotation.'
        : '3D mapping context loaded. The mapped range is loaded into a new annotation draft.'
    )
    setTimeout(() => {
      modelEditorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 0)
  }, [pendingAnnotationContext, mode, modelProjectId, modelObjectId, modelAnnotations])

  useEffect(() => {
    if (!annotationMappingPrompt || annotationMappingTargetId || !modelAnnotations.length) {
      return
    }
    setAnnotationMappingTargetId(modelAnnotations[0].id)
  }, [annotationMappingPrompt, annotationMappingTargetId, modelAnnotations])

  useEffect(() => {
    if (!token || !objectEdit.id) {
      setOpsObjectModel(null)
      setOpsObjectModelFile(null)
      return
    }

    loadOpsObjectModel(objectEdit.id, token).catch(() => null)
  }, [objectEdit.id, token])

  useEffect(() => {
    if (!token || mode !== 'model3d' || !modelObjectId) {
      return
    }

    loadModelWorkspace(modelObjectId, token).catch(() => null)
  }, [mode, modelObjectId, token])

  useEffect(() => {
    if (!token || IS_PUBLIC_APP || mode !== 'model3d') {
      return
    }

    loadLiveDatabaseSyncStatus(token, { silent: true }).catch(() => null)
  }, [mode, token])

  useEffect(() => {
    if (!token || IS_PUBLIC_APP || mode !== 'model3d' || liveDatabaseSyncStatus.state !== 'RUNNING') {
      return undefined
    }

    const intervalId = window.setInterval(() => {
      loadLiveDatabaseSyncStatus(token, { silent: true }).catch(() => null)
    }, 2500)

    return () => window.clearInterval(intervalId)
  }, [liveDatabaseSyncStatus.operation_id, liveDatabaseSyncStatus.state, mode, token])

  useEffect(() => {
    const previousState = previousLiveDatabaseSyncStateRef.current
    if (previousState === 'RUNNING' && liveDatabaseSyncStatus.state === 'SUCCEEDED') {
      setNotice(liveDatabaseSyncStatus.detail || 'Live Database sync complete.')
    }
    if (previousState === 'RUNNING' && liveDatabaseSyncStatus.state === 'FAILED') {
      setError(liveDatabaseSyncStatus.error || liveDatabaseSyncStatus.detail || 'Live Database sync failed.')
    }
    previousLiveDatabaseSyncStateRef.current = liveDatabaseSyncStatus.state
  }, [liveDatabaseSyncStatus])

  useEffect(() => {
    if (!modelObjectId) {
      setPublicPreviewData(null)
      setPublicPreviewAnnotationId('')
      setPublicPreviewClipIndex(0)
      setPublicPreviewClipPlayback(null)
      setPublicPreviewPendingClipCommit(null)
      setPublicPreviewSearchMoment(null)
      setPublicPreviewCurrentMs(0)
      setPublicPreviewPendingPlayback(null)
      setPublicPreviewShouldScrollViewerIntoView(false)
      return
    }

    setPublicPreviewVideoId((current) => {
      if (!current) {
        return ''
      }
      const stillLinked = sortedVideos.some((video) => video.id === current && video.object_id === modelObjectId)
      return stillLinked ? current : ''
    })
  }, [modelObjectId, sortedVideos])

  useEffect(() => {
    if ((!token && !IS_PUBLIC_APP) || mode !== 'publicPreview' || !modelObjectId) {
      return
    }

    loadPublicPreview(modelObjectId, token).catch((err) => setError(err.message))
  }, [mode, modelObjectId, publicPreviewVideoId, token, sortedObjects, sortedVideos])

  useEffect(() => {
    setModelViewMode(false)
    setModelTransformDraft(defaultModelTransform(modelDetail))
    setModelCameraDraft(savedModelCameraView)
  }, [modelDetail?.id, modelDetail?.updated_at, savedModelCameraView])

  useEffect(() => {
    if (!selectedModelAnnotation) {
      setSelectedModelAnnotationUrl('')
      setAnnotationOverlayTranscript(null)
      setAnnotationOverlaySegments([])
      setAnnotationOverlayCurrentMs(0)
      setAnnotationOverlayClipIndex(0)
      setAnnotationOverlayEnded(false)
      return
    }

    if (!token || !annotationOverlayClipVideo) {
      setSelectedModelAnnotationUrl('')
      return
    }

    if (annotationOverlayClipVideo.status !== 'READY') {
      setSelectedModelAnnotationUrl('')
      return
    }

    try {
      setSelectedModelAnnotationUrl(getVideoPlaybackUrl(token, annotationOverlayClipVideo.id, annotationOverlayClipVideo.updated_at))
    } catch {
      setSelectedModelAnnotationUrl('')
    }
  }, [token, selectedModelAnnotation, annotationOverlayClipVideo])

  useEffect(() => {
    if (!selectedModelAnnotation) {
      return
    }

    const refreshed = modelAnnotations.find((annotation) => annotation.id === selectedModelAnnotation.id) || null
    if (!refreshed) {
      setSelectedModelAnnotation(null)
      setAnnotationOverlayOpen(false)
      setAnnotationOverlayClipIndex(0)
      return
    }

    if (refreshed !== selectedModelAnnotation) {
      setSelectedModelAnnotation(refreshed)
    }
  }, [modelAnnotations, selectedModelAnnotation])

  useEffect(() => {
    if (!publicPreviewData) {
      setPublicPreviewAnnotationId('')
      setPublicPreviewClipIndex(0)
      setPublicPreviewClipPlayback(null)
      setPublicPreviewPendingClipCommit(null)
      setPublicPreviewSearchMoment(null)
      setPublicPreviewCurrentMs(0)
      return
    }

    setPublicPreviewAnnotationId((current) => {
      if (current && publicPreviewData.annotations.some((annotation) => annotation.id === current)) {
        return current
      }
      if (publicPreviewSearchMoment?.objectId === publicPreviewData.object?.id) {
        return ''
      }
      return publicPreviewData.annotations[0]?.id || ''
    })
  }, [publicPreviewData, publicPreviewSearchMoment?.objectId])

  useEffect(() => {
    setPublicPreviewClipIndex(0)
  }, [publicPreviewAnnotationId])

  useEffect(() => {
    if (!annotationOverlayOpen || !annotationOverlayClip || !token) {
      return
    }

    let cancelled = false

    async function loadAnnotationOverlay() {
      setAnnotationOverlayLoading(true)
      setAnnotationOverlayError('')
      setAnnotationOverlayTranscript(null)
      setAnnotationOverlaySegments([])

      try {
        const transcriptData = await getVideoTranscript(token, annotationOverlayClip.video_id)
        if (cancelled) {
          return
        }
        setAnnotationOverlayTranscript(transcriptData.transcript)
        setAnnotationOverlaySegments(transcriptData.segments || [])
      } catch (err) {
        if (cancelled) {
          return
        }
        const isTranscriptMissing = /transcript.*not.*found|no transcript/i.test(String(err?.message || ''))
        if (isTranscriptMissing) {
          setAnnotationOverlayTranscript(null)
          setAnnotationOverlaySegments([])
        } else {
          setAnnotationOverlayError(err.message)
        }
      } finally {
        if (!cancelled) {
          setAnnotationOverlayCurrentMs(Math.max(0, Number(annotationOverlayClip.start_ms || 0)))
          setAnnotationOverlayEnded(false)
          setAnnotationOverlayLoading(false)
        }
      }
    }

    loadAnnotationOverlay()

    return () => {
      cancelled = true
    }
  }, [annotationOverlayOpen, annotationOverlayClip, token])

  useEffect(() => {
    if (!publicPreviewTranscriptListRef.current || publicPreviewCurrentSegmentIndex < 0) {
      return
    }

    const target = publicPreviewTranscriptListRef.current.querySelector(
      `[data-public-preview-segment-index="${publicPreviewCurrentSegmentIndex}"]`
    )
    if (!target) {
      return
    }

    const containerRect = publicPreviewTranscriptListRef.current.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const isOutOfView = targetRect.top < containerRect.top + 8 || targetRect.bottom > containerRect.bottom - 8

    if (isOutOfView) {
      target.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [publicPreviewCurrentSegmentIndex])

  useEffect(() => {
    if (
      mode !== 'publicPreview'
      || !publicPreviewShouldScrollViewerIntoView
      || !publicPreviewData
      || !publicPreviewViewerRef.current
    ) {
      return
    }

    window.requestAnimationFrame(() => {
      publicPreviewViewerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      setPublicPreviewShouldScrollViewerIntoView(false)
    })
  }, [mode, publicPreviewData, publicPreviewShouldScrollViewerIntoView])

  useEffect(() => {
    if (!annotationOverlayOpen) {
      return
    }

    function handleKeydown(event) {
      if (event.key === 'Escape') {
        setAnnotationOverlayOpen(false)
      }
    }

    window.addEventListener('keydown', handleKeydown)
    return () => window.removeEventListener('keydown', handleKeydown)
  }, [annotationOverlayOpen])

  useEffect(() => {
    if (!annotationOverlayOpen || !annotationOverlayClip || !selectedModelAnnotationUrl) {
      return
    }

    const media = annotationOverlayVideoRef.current
    if (!media || media.readyState < 1) {
      return
    }

    const clipStartMs = Math.max(0, Number(annotationOverlayClip.start_ms || 0))
    media.currentTime = clipStartMs / 1000
    setAnnotationOverlayCurrentMs(clipStartMs)
    setAnnotationOverlayEnded(false)
    media.play().catch(() => null)
  }, [annotationOverlayOpen, annotationOverlayClipIndex, annotationOverlayClip, selectedModelAnnotationUrl])

  useEffect(() => {
    if (!viewerVideoId && sortedVideos.length) {
      setViewerVideoId(sortedVideos[0].id)
    }
  }, [viewerVideoId, sortedVideos])

  useEffect(() => {
    if (!token || !viewerVideoId || mode !== 'analysis') return

    let cancelled = false

    async function loadViewerData() {
      setViewerLoading(true)
      setError('')
      setViewerMediaUrl('')
      try {
        if (activeVideo?.status === 'READY') {
          const mediaUrl = await getVideoPlaybackUrl(token, viewerVideoId, activeVideo?.updated_at)
          if (!cancelled) {
            setViewerMediaUrl(mediaUrl)
          }
        } else {
          setViewerMediaUrl('')
        }

          const transcriptData = await getVideoTranscript(token, viewerVideoId)
          if (cancelled) {
            return
          }
        setViewerTranscript(transcriptData.transcript)
        setViewerSegments(transcriptData.segments || [])
        const draftSource = viewerDraftSourceRef.current
        if (draftSource?.videoId !== viewerVideoId || draftSource?.token !== token) {
          setViewerDraftText(transcriptData.transcript.raw_text)
          initializeViewerHistory(transcriptData.transcript.raw_text)
          viewerDraftSourceRef.current = { videoId: viewerVideoId, token }
        }
          setViewerCurrentMs((current) => (
            current > 0 && analysisSessionRef.current?.videoId === viewerVideoId ? current : 0
          ))
      } catch (err) {
        if (!cancelled) {
            const isTranscriptMissing = /transcript.*not.*found|no transcript/i.test(String(err?.message || ''))
            if (activeVideo?.status === 'READY' && isTranscriptMissing) {
              setViewerTranscript(null)
              setViewerSegments([])
              if (viewerDraftSourceRef.current?.videoId !== viewerVideoId || viewerDraftSourceRef.current?.token !== token) {
                setViewerDraftText('')
                initializeViewerHistory('')
                viewerDraftSourceRef.current = null
              }
              setNotice('Transcript is still finalizing. Retrying automatically...')
              setTimeout(() => {
                refreshData(token).catch(() => null)
              }, 4000)
              return
            }
          setViewerTranscript(null)
          setViewerSegments([])
          if (viewerDraftSourceRef.current?.videoId !== viewerVideoId || viewerDraftSourceRef.current?.token !== token) {
            setViewerDraftText('')
            initializeViewerHistory('')
            viewerDraftSourceRef.current = null
          }
          setViewerMediaUrl('')
          setError(err.message)
        }
      } finally {
        if (!cancelled) {
          setViewerLoading(false)
        }
      }
    }

    loadViewerData()

    return () => {
      cancelled = true
    }
  }, [token, mode, viewerVideoId, activeVideo?.status, activeVideo?.updated_at])

  useEffect(() => {
    const previousMode = previousModeRef.current

    if (previousMode === 'analysis' && mode !== 'analysis' && viewerVideoId) {
      analysisSessionRef.current = {
        videoId: viewerVideoId,
        seekMs: Math.max(0, Math.floor(viewerCurrentMs || 0))
      }
    }

    if (previousMode !== 'analysis' && mode === 'analysis') {
      const checkpoint = analysisSessionRef.current
      if (checkpoint?.videoId && checkpoint.videoId === viewerVideoId) {
        setPendingPlayback({
          videoId: checkpoint.videoId,
          seekMs: checkpoint.seekMs,
          autoplay: false
        })
      }
    }

    previousModeRef.current = mode
  }, [mode, viewerCurrentMs, viewerVideoId])

  useEffect(() => {
    if (mode !== 'analysis' || !analysisFocusResult) {
      return
    }

    if (analysisFocusResult.video_id !== viewerVideoId) {
      setAnalysisFocusResult(null)
    }
  }, [analysisFocusResult, mode, viewerVideoId])

  useEffect(() => {
    if (!token || !videos.some((video) => video.status === 'TRANSCODING')) return
    const intervalId = setInterval(() => {
      refreshData(token).catch(() => null)
    }, 5000)
    return () => clearInterval(intervalId)
  }, [token, videos])

  useEffect(() => {
    if (!token || !recentClips.some((clip) => clip.status === 'QUEUED' || clip.status === 'PROCESSING')) return
    const intervalId = setInterval(() => {
      refreshClips(token).catch(() => null)
    }, 3000)
    return () => clearInterval(intervalId)
  }, [token, recentClips])

  useEffect(() => {
    const hasTranscoding = videos.some((video) => video.status === 'TRANSCODING')
    if (!hasTranscoding) return
    const intervalId = setInterval(() => setStatusNowMs(Date.now()), 1000)
    return () => clearInterval(intervalId)
  }, [videos])

  useEffect(() => {
    if (!transcriptListRef.current || currentSegmentIndex < 0) return

    const target = transcriptListRef.current.querySelector(`[data-segment-index="${currentSegmentIndex}"]`)
    if (!target) return

    const containerRect = transcriptListRef.current.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const isOutOfView = targetRect.top < containerRect.top + 8 || targetRect.bottom > containerRect.bottom - 8

    if (isOutOfView) {
      target.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [currentSegmentIndex])

  useEffect(() => {
    if (
      !annotationOverlayOpen
      || annotationOverlayPanel !== 'transcript'
      || !annotationOverlayTranscriptListRef.current
      || annotationOverlaySegmentIndex < 0
    ) {
      return
    }

    const target = annotationOverlayTranscriptListRef.current.querySelector(
      `[data-overlay-segment-index="${annotationOverlaySegmentIndex}"]`
    )
    if (!target) return

    const containerRect = annotationOverlayTranscriptListRef.current.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const isOutOfView = targetRect.top < containerRect.top + 8 || targetRect.bottom > containerRect.bottom - 8

    if (isOutOfView) {
      target.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [annotationOverlayOpen, annotationOverlayPanel, annotationOverlaySegmentIndex])

  useEffect(() => {
    if (!videoRef.current || !pendingPlayback || !viewerMediaUrl) return
    applyPendingPlayback(videoRef.current)
  }, [pendingPlayback, viewerMediaUrl, viewerVideoId])

  useEffect(() => {
    if (!publicPreviewVideoRef.current || !publicPreviewPendingPlayback || !publicPreviewData?.videoUrl) {
      return
    }
    applyPublicPreviewPendingPlayback(publicPreviewVideoRef.current)
  }, [publicPreviewData?.video?.id, publicPreviewData?.videoUrl, publicPreviewPendingPlayback])

  async function handleLogin(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const response = await login(email, password)
      localStorage.setItem(TOKEN_KEY, response.access_token)
      setToken(response.access_token)
      setNotice('Authenticated. Console is ready.')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function handleLogout() {
    localStorage.removeItem(TOKEN_KEY)
    setToken('')
    setProjects([])
    setObjects([])
    setVideos([])
    setTranscripts([])
    setSearchFeedbackBySegmentId({})
    setSearchFeedbackBusyBySegmentId({})
    setTuningSummary(null)
    setPublicPreviewData(null)
    setPublicPreviewVideoId('')
    setPublicPreviewPendingPlayback(null)
    setPublicPreviewAnnotationId('')
    setNotice('Logged out.')
  }

  async function withSubmit(action, successMessage) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await action()
      await refreshData()
      setNotice(successMessage)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleMediaUpload() {
    if (!mediaUpload) {
      setError('Choose a media file to upload first.')
      return
    }
    if (!videoForm.project_id) {
      setError('Select a project before upload so upload/register/transcode can run in one step.')
      return
    }
    setMediaUploading(true)
    setMediaUploadProgress(0)
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const uploaded = await uploadMediaFile(token, mediaUpload, (progress) => setMediaUploadProgress(progress))
      const stableId = videoForm.stable_video_id || nextStableVideoId()
      const createResult = await createVideo(token, {
        project_id: videoForm.project_id,
        object_id: videoForm.object_id || null,
        stable_video_id: stableId,
        title: videoForm.title || uploaded.filename || 'Untitled video',
        source_path: uploaded.path,
        original_filename: uploaded.filename,
        enqueue_transcode: true,
        auto_transcribe_if_missing: true
      })

      await refreshData()
      await refreshClips(token)
      setVideoForm((prev) => ({
        ...prev,
        stable_video_id: nextStableVideoId(),
        title: '',
        media_path: uploaded.path
      }))
      setNotice(
        `Uploaded and registered ${uploaded.filename}. Transcode job: ${createResult.transcode_job_id || 'queue unavailable'}.`
      )
      setMediaUpload(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setMediaUploading(false)
      setMediaUploadProgress(0)
      setBusy(false)
    }
  }

  async function saveProjectEdits() {
    if (!projectEdit.id || !projectEdit.name.trim()) {
      setError('Project name is required to save edits.')
      return
    }

    await withSubmit(
      () =>
        updateProject(token, projectEdit.id, {
          name: projectEdit.name.trim(),
          description: projectEdit.description?.trim() || null
        }),
      'Project updated.'
    )
    setProjectEdit({ id: '', name: '', description: '' })
  }

  async function saveObjectEdits() {
    if (!objectEdit.id || !objectEdit.name.trim()) {
      setError('Object name is required to save edits.')
      return
    }

    let metadata = null
    if (objectEdit.metadata_json.trim()) {
      try {
        metadata = JSON.parse(objectEdit.metadata_json)
      } catch {
        setError('Object metadata must be valid JSON.')
        return
      }
    }

    await withSubmit(
      () =>
        updateObject(token, objectEdit.id, {
          name: objectEdit.name.trim(),
          description: objectEdit.description?.trim() || null,
          metadata_json: metadata
        }),
      'Object updated.'
    )
    setObjectEdit({ id: '', name: '', description: '', metadata_json: '' })
  }

  function beginObjectEdit(objectRow) {
    if (!objectRow) {
      setObjectEdit({ id: '', name: '', description: '', metadata_json: '' })
      return
    }

    setObjectEdit({
      id: objectRow.id,
      name: objectRow.name,
      description: objectRow.description || '',
      metadata_json: objectRow.metadata_json ? JSON.stringify(objectRow.metadata_json, null, 2) : ''
    })
  }

  async function saveVideoEdits() {
    if (!videoEdit.id || !videoEdit.title.trim()) {
      setError('Video title is required to save edits.')
      return
    }

    await withSubmit(
      () =>
        updateVideo(token, videoEdit.id, {
          title: videoEdit.title.trim(),
          object_id: videoEdit.object_id || null
        }),
      'Video updated.'
    )
    setVideoEdit({ id: '', title: '', object_id: '' })
  }

  async function saveTranscriptEdits() {
    if (!transcriptEdit.id || !transcriptEdit.title.trim()) {
      setError('Transcript title is required to save edits.')
      return
    }

    await withSubmit(
      () => updateTranscript(token, transcriptEdit.id, { title: transcriptEdit.title.trim() }),
      'Transcript updated.'
    )
    setTranscriptEdit({ id: '', title: '' })
  }

  async function loadOpsObjectModel(objectId, activeToken = token) {
    try {
      const model = await getObjectModel(activeToken, objectId)
      setOpsObjectModel(model)
    } catch (err) {
      if (/object model not found/i.test(err.message)) {
        setOpsObjectModel(null)
        return
      }
      throw err
    }
  }

  async function loadModelWorkspace(objectId, activeToken = token) {
    if (!objectId) {
      setModelDetail(null)
      setModelAnnotations([])
      return
    }

    const requestId = modelWorkspaceLoadIdRef.current + 1
    modelWorkspaceLoadIdRef.current = requestId

    setModelLoading(true)
    try {
      const [model, annotations] = await Promise.all([
        getObjectModel(activeToken, objectId).catch((err) => {
          if (/object model not found/i.test(err.message)) {
            return null
          }
          throw err
        }),
        getObjectModelAnnotations(activeToken, objectId).catch((err) => {
          if (/object model not found/i.test(err.message)) {
            return []
          }
          throw err
        })
      ])

      if (modelWorkspaceLoadIdRef.current !== requestId) {
        return
      }

      setModelDetail(model)
      setModelAnnotations(annotations || [])
    } finally {
      if (modelWorkspaceLoadIdRef.current === requestId) {
        setModelLoading(false)
      }
    }
  }

  async function loadPublicPreview(objectId, activeToken = token) {
    if (!objectId || (!activeToken && !IS_PUBLIC_APP)) {
      setPublicPreviewData(null)
      return
    }

    const requestId = publicPreviewLoadIdRef.current + 1
    publicPreviewLoadIdRef.current = requestId

    setPublicPreviewLoading(true)
    try {
      const objectRow = sortedObjects.find((candidate) => candidate.id === objectId) || null
      if (!IS_PUBLIC_APP && !objectRow?.is_published) {
        if (publicPreviewLoadIdRef.current === requestId) {
          setPublicPreviewData(null)
        }
        return
      }

      const payload = await getPublicObjectPreview(objectId, {
        videoId: publicPreviewVideoId || null
      })

      if (publicPreviewLoadIdRef.current !== requestId) {
        return
      }

      setPublicPreviewData({
        ...payload,
        object: buildPublicSurfaceObject(payload.object),
        modelUrl: resolveApiUrl(payload.model_url || ''),
        videoUrl: resolveApiUrl(payload.video_url || '')
      })
    } finally {
      if (publicPreviewLoadIdRef.current === requestId) {
        setPublicPreviewLoading(false)
      }
    }
  }

  async function uploadModelForObject(objectId, file) {
    if (!objectId) {
      setError('Select an object before uploading a .glb model.')
      return
    }
    if (!file) {
      setError('Choose a .glb file first.')
      return
    }

    setModelBusy(true)
    setError('')
    setNotice('')
    try {
      const model = await uploadObjectModel(token, objectId, file)
      const objectRow = sortedObjects.find((candidate) => candidate.id === objectId) || null
      if (objectRow) {
        setModelProjectId(objectRow.project_id)
        setModelObjectId(objectRow.id)
      }
      if (objectEdit.id === objectId) {
        setOpsObjectModel(model)
        setOpsObjectModelFile(null)
        if (opsModelFileInputRef.current) {
          opsModelFileInputRef.current.value = ''
        }
      }
      if (modelObjectId === objectId) {
        setModelUploadFile(null)
        if (modelFileInputRef.current) {
          modelFileInputRef.current.value = ''
        }
        await loadModelWorkspace(objectId, token)
      }
      setNotice(
        model.revision_number > 1
          ? `Model replaced. Revision ${model.revision_number} is active and existing annotations now require manual review.`
          : `Model uploaded. Revision ${model.revision_number} is active for this object.`
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  async function removeModelForObject(objectId) {
    if (!objectId) {
      return
    }
    if (!window.confirm('Delete the current .glb model for this object? Existing 3D annotations will also be removed.')) {
      return
    }

    setModelBusy(true)
    setError('')
    try {
      await deleteObjectModel(token, objectId)
      if (objectEdit.id === objectId) {
        setOpsObjectModel(null)
        setOpsObjectModelFile(null)
        if (opsModelFileInputRef.current) {
          opsModelFileInputRef.current.value = ''
        }
      }
      if (modelObjectId === objectId) {
        setModelDetail(null)
        setModelAnnotations([])
        setModelUploadFile(null)
        if (modelFileInputRef.current) {
          modelFileInputRef.current.value = ''
        }
        setModelViewMode(false)
        setModelTransformDraft(defaultModelTransform())
        setModelCameraDraft(null)
        setPlacementMode(false)
        setSelectedModelAnnotation(null)
        setAnnotationOverlayOpen(false)
      }
      setNotice('Object model removed.')
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  async function saveModelView() {
    if (!modelDetail || !modelObjectId) {
      setError('Upload a .glb model before editing its saved orientation.')
      return
    }

    const nextTransform = defaultModelTransform(modelTransformDraft)
    const nextCameraView = normalizeModelCameraView(modelCameraDraft)
    if (Object.values(nextTransform).some((value) => !Number.isFinite(value))) {
      setError('Model transform values must remain numeric during editing.')
      return
    }

    setModelBusy(true)
    setError('')
    try {
      await updateObjectModelView(token, modelObjectId, { ...nextTransform, default_camera_json: nextCameraView })
      await loadModelWorkspace(modelObjectId, token)
      setModelViewMode(false)
      setNotice('Default 3D model orientation saved.')
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  function handleModelTransformChange(nextTransform) {
    setModelTransformDraft(defaultModelTransform(nextTransform))
  }

  function handleModelCameraViewChange(nextCameraView) {
    const normalizedCameraView = normalizeModelCameraView(nextCameraView)
    setModelCameraDraft((current) => {
      if (cameraViewsEqual(current, normalizedCameraView)) {
        return current
      }
      return normalizedCameraView
    })
  }

  function startModelAnnotationPlacement() {
    if (!modelDetail) {
      setError('Upload a .glb model before placing annotations.')
      return
    }
    setModelViewMode(false)
    setAnnotationOverlayOpen(false)
    setPlacementMode(true)
    setNotice('Placement mode active. Click the model surface to capture an annotation point.')
  }

  function selectModelAnnotation(annotation, { loadIntoEditor = false, openOverlay = false } = {}) {
    setSelectedModelAnnotation(annotation)
    setPlacementMode(false)
    if (loadIntoEditor) {
      setAnnotationEdit(annotationDraftFromAnnotation(annotation))
      setTimeout(() => {
        modelEditorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }, 0)
    }
    if (openOverlay) {
      setAnnotationOverlayPanel('transcript')
      setAnnotationOverlayOpen(true)
    }
  }

  function closeAnnotationOverlay() {
    setAnnotationOverlayOpen(false)
  }

  function openModelAnnotationOverlay(annotation) {
    if (!annotation) {
      return
    }
    setSelectedModelAnnotation(annotation)
    setPlacementMode(false)
    setAnnotationOverlayError('')
    setAnnotationOverlayPanel('transcript')
    setAnnotationOverlayClipIndex(0)
    setAnnotationOverlayEnded(false)
    setAnnotationOverlayOpen(true)
  }

  function jumpAnnotationOverlayToMs(targetMs, autoplay = true) {
    const media = annotationOverlayVideoRef.current
    const clipEndMs = Math.max(0, Number(annotationOverlayClip?.end_ms || 0))
    const nextMs = Math.max(0, Math.min(Math.floor(targetMs || 0), clipEndMs || Number.POSITIVE_INFINITY))
    setAnnotationOverlayEnded(false)
    setAnnotationOverlayCurrentMs(nextMs)
    if (!media) {
      return
    }

    media.currentTime = nextMs / 1000
    if (autoplay) {
      media.play().catch(() => null)
    }
  }

  function syncAnnotationDraftPrimaryClip(entry, playlist) {
    return {
      video_id: entry.video_id,
      clip_id: entry.clip_id || '',
      transcript_segment_id: entry.transcript_segment_id || '',
      start_ms: String(entry.start_ms),
      end_ms: String(entry.end_ms),
      playlist
    }
  }

  function buildAnnotationSequenceEntry(videoId, startMs, endMs, sequenceLength, extra = {}) {
    return {
      video_id: videoId,
      clip_id: extra.clip_id || null,
      transcript_segment_id: extra.transcript_segment_id || null,
      label: extra.label || `Clip ${sequenceLength + 1}`,
      start_ms: Math.floor(startMs),
      end_ms: Math.floor(endMs)
    }
  }

  function startNewAnnotationFromMappingPrompt() {
    if (!annotationMappingPrompt) {
      return
    }
    setSelectedModelAnnotation(null)
    setAnnotationMappingPrompt(null)
    setAnnotationMappingTargetId('')

    if (modelDetail) {
      setPlacementMode(true)
      setNotice('Create a new annotation selected. Click the model surface to place the new pin.')
    } else {
      setNotice('Create a new annotation selected. Upload a model for this object, then place the pin.')
    }
  }

  function addMappedRangeToExistingAnnotation() {
    if (!annotationMappingPrompt) {
      return
    }
    if (!annotationMappingTargetId) {
      setError('Choose an existing annotation before adding this range to a sequence.')
      return
    }

    const targetAnnotation = modelAnnotations.find((annotation) => annotation.id === annotationMappingTargetId)
    if (!targetAnnotation) {
      setError('The selected annotation could not be found. Reload the object workspace and try again.')
      return
    }

    const draft = annotationDraftFromAnnotation(targetAnnotation)
    const nextPlaylist = [
      ...draft.playlist,
      buildAnnotationSequenceEntry(
        annotationMappingPrompt.videoId,
        annotationMappingPrompt.startMs,
        annotationMappingPrompt.endMs,
        draft.playlist.length
      )
    ]

    setSelectedModelAnnotation(targetAnnotation)
    setPlacementMode(false)
    setAnnotationEdit({
      ...draft,
      ...syncAnnotationDraftPrimaryClip(nextPlaylist[0], nextPlaylist)
    })
    setAnnotationMappingPrompt(null)
    setAnnotationMappingTargetId('')
    setNotice('Mapped range added to the selected annotation sequence. Review the sequence, then save the annotation.')
    setTimeout(() => {
      modelEditorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 0)
  }

  function addCurrentClipToAnnotationSequence() {
    if (!annotationEdit.video_id) {
      setError('Select a linked video before adding a clip to the annotation sequence.')
      return
    }

    const startMs = Number(annotationEdit.start_ms)
    const endMs = Number(annotationEdit.end_ms)
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      setError('Set a valid start/end range before adding a clip to the annotation sequence.')
      return
    }

    setAnnotationEdit((prev) => {
      const nextPlaylist = [
        ...prev.playlist,
        {
          video_id: prev.video_id,
          clip_id: prev.clip_id || null,
          transcript_segment_id: prev.transcript_segment_id || null,
          label: `Clip ${prev.playlist.length + 1}`,
          start_ms: Math.floor(startMs),
          end_ms: Math.floor(endMs)
        }
      ]
      return { ...prev, playlist: nextPlaylist }
    })
    setNotice('Clip added to annotation sequence.')
  }

  function moveAnnotationSequenceClip(index, direction) {
    setAnnotationEdit((prev) => {
      const nextIndex = index + direction
      if (nextIndex < 0 || nextIndex >= prev.playlist.length) {
        return prev
      }
      const nextPlaylist = [...prev.playlist]
      ;[nextPlaylist[index], nextPlaylist[nextIndex]] = [nextPlaylist[nextIndex], nextPlaylist[index]]
      const relabeled = nextPlaylist.map((entry, entryIndex) => ({ ...entry, label: entry.label || `Clip ${entryIndex + 1}` }))
      const primary = relabeled[0]
      return {
        ...prev,
        ...syncAnnotationDraftPrimaryClip(primary, relabeled)
      }
    })
  }

  function removeAnnotationSequenceClip(index) {
    setAnnotationEdit((prev) => {
      const nextPlaylist = prev.playlist.filter((_, entryIndex) => entryIndex !== index)
      if (!nextPlaylist.length) {
        return { ...prev, playlist: [] }
      }
      const primary = nextPlaylist[0]
      return {
        ...prev,
        ...syncAnnotationDraftPrimaryClip(primary, nextPlaylist)
      }
    })
  }

  function promoteAnnotationSequenceClip(index) {
    setAnnotationEdit((prev) => {
      if (index < 0 || index >= prev.playlist.length) {
        return prev
      }
      const promoted = prev.playlist[index]
      const remaining = prev.playlist.filter((_, entryIndex) => entryIndex !== index)
      const nextPlaylist = [promoted, ...remaining]
      return {
        ...prev,
        ...syncAnnotationDraftPrimaryClip(promoted, nextPlaylist)
      }
    })
  }

  function finishAnnotationOverlayClip() {
    const media = annotationOverlayVideoRef.current
    if (!annotationOverlayClip || !media) {
      return
    }

    if (annotationOverlayClipIndex + 1 < selectedModelAnnotationPlaylist.length) {
      setAnnotationOverlayClipIndex((current) => current + 1)
      setAnnotationOverlayEnded(false)
      return
    }

    const clipStartMs = Math.max(0, Number(annotationOverlayClip.start_ms || 0))
    media.pause()
    media.currentTime = clipStartMs / 1000
    setAnnotationOverlayCurrentMs(clipStartMs)
    setAnnotationOverlayEnded(true)
  }

  function handleModelSurfacePick(payload) {
    const nextVideoId = annotationEdit.video_id || filteredVideosForModelTab[0]?.id || ''
    setAnnotationEdit((prev) =>
      emptyAnnotationDraft({
        ...prev,
        id: prev.id,
        title: prev.title,
        description: prev.description,
        video_id: nextVideoId,
        start_ms: prev.start_ms,
        end_ms: prev.end_ms,
        point_x: payload.point.x.toFixed(6),
        point_y: payload.point.y.toFixed(6),
        point_z: payload.point.z.toFixed(6),
        normal_x: payload.normal ? payload.normal.x.toFixed(6) : '',
        normal_y: payload.normal ? payload.normal.y.toFixed(6) : '',
        normal_z: payload.normal ? payload.normal.z.toFixed(6) : ''
      })
    )
    setPlacementMode(false)
    setSelectedModelAnnotation(null)
    setAnnotationOverlayOpen(false)
    setNotice('Annotation point captured. Complete the form and save it to this model.')
  }

  async function saveModelAnnotation() {
    if (!modelObjectId || !modelDetail) {
      setError('Select an object with an uploaded model first.')
      return
    }
    if (!annotationEdit.title.trim()) {
      setError('Annotation title is required.')
      return
    }
    if (!annotationEdit.video_id) {
      setError('Select a linked video for this annotation.')
      return
    }

    const startMs = Number(annotationEdit.start_ms)
    const endMs = Number(annotationEdit.end_ms)
    const pointX = Number(annotationEdit.point_x)
    const pointY = Number(annotationEdit.point_y)
    const pointZ = Number(annotationEdit.point_z)
    const playlist = annotationEdit.playlist.length
      ? annotationEdit.playlist
      : [{
          video_id: annotationEdit.video_id,
          clip_id: annotationEdit.clip_id || null,
          transcript_segment_id: annotationEdit.transcript_segment_id || null,
          label: 'Clip 1',
          start_ms: Math.floor(startMs),
          end_ms: Math.floor(endMs)
        }]

    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      setError('Annotation start and end times must be valid, and end must be greater than start.')
      return
    }
    if (!Number.isFinite(pointX) || !Number.isFinite(pointY) || !Number.isFinite(pointZ)) {
      setError('Click the model to capture a valid annotation point before saving.')
      return
    }

    const payload = {
      title: annotationEdit.title.trim(),
      description: annotationEdit.description.trim() || null,
      video_id: annotationEdit.video_id,
      clip_id: annotationEdit.clip_id || null,
      transcript_segment_id: annotationEdit.transcript_segment_id || null,
      start_ms: Math.floor(startMs),
      end_ms: Math.floor(endMs),
      point_x: pointX,
      point_y: pointY,
      point_z: pointZ,
      normal_x: annotationEdit.normal_x === '' ? null : Number(annotationEdit.normal_x),
      normal_y: annotationEdit.normal_y === '' ? null : Number(annotationEdit.normal_y),
      normal_z: annotationEdit.normal_z === '' ? null : Number(annotationEdit.normal_z),
      playlist
    }

    setModelBusy(true)
    setError('')
    try {
      let savedAnnotation = null
      if (annotationEdit.id) {
        savedAnnotation = await updateObjectModelAnnotation(token, annotationEdit.id, payload)
        setNotice('3D annotation updated.')
      } else {
        savedAnnotation = await createObjectModelAnnotation(token, modelObjectId, payload)
        setNotice('3D annotation created.')
      }
      await loadModelWorkspace(modelObjectId, token)
      if (savedAnnotation) {
        selectModelAnnotation(savedAnnotation, { loadIntoEditor: true })
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  async function markAnnotationReviewed(annotationId) {
    setModelBusy(true)
    setError('')
    try {
      await markObjectModelAnnotationReviewed(token, annotationId)
      await loadModelWorkspace(modelObjectId, token)
      setNotice('Annotation marked as reviewed.')
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  async function removeModelAnnotation(annotationId) {
    if (!window.confirm('Delete this 3D annotation?')) {
      return
    }
    setModelBusy(true)
    setError('')
    try {
      await deleteObjectModelAnnotation(token, annotationId)
      await loadModelWorkspace(modelObjectId, token)
      if (annotationEdit.id === annotationId) {
        setAnnotationEdit(emptyAnnotationDraft({ video_id: filteredVideosForModelTab[0]?.id || '' }))
      }
      if (selectedModelAnnotation?.id === annotationId) {
        setSelectedModelAnnotation(null)
        setAnnotationOverlayOpen(false)
      }
      setNotice('3D annotation deleted.')
    } catch (err) {
      setError(err.message)
    } finally {
      setModelBusy(false)
    }
  }

  async function loadLiveDatabaseSyncStatus(activeToken = token, { silent = false } = {}) {
    if (!activeToken || IS_PUBLIC_APP) {
      const idleStatus = emptyLiveDatabaseSyncStatus()
      setLiveDatabaseSyncStatus(idleStatus)
      setLiveDatabaseSyncRequestBusy(false)
      return idleStatus
    }

    try {
      const payload = await getLiveDatabaseSyncStatus(activeToken)
      setLiveDatabaseSyncStatus(payload)
      return payload
    } catch (err) {
      if (!silent) {
        setError(err.message)
      }
      return null
    } finally {
      setLiveDatabaseSyncRequestBusy(false)
    }
  }

  async function withPublicationUpdate(action, successMessage) {
    setPublicationBusy(true)
    setError('')
    setNotice('')
    try {
      await action()
      await refreshData(token)
      if (modelObjectId) {
        await loadModelWorkspace(modelObjectId, token)
      }
      if (objectEdit.id) {
        await loadOpsObjectModel(objectEdit.id, token)
      }
      if (mode === 'publicPreview' && modelObjectId) {
        await loadPublicPreview(modelObjectId, token)
      }
      setNotice(successMessage)
    } catch (err) {
      setError(err.message)
    } finally {
      setPublicationBusy(false)
    }
  }

  async function setObjectPublished(nextPublished) {
    if (!activeModelObject) {
      setError('Select an object before changing publication state.')
      return
    }

    await withPublicationUpdate(
      () => updateObject(token, activeModelObject.id, { is_published: nextPublished }),
      nextPublished ? 'Object published to Public Preview.' : 'Object hidden from Public Preview.'
    )
  }

  async function setModelPublished(nextPublished) {
    if (!activeModelObject || !modelDetail || !modelObjectId) {
      setError('Upload a model before changing its publication state.')
      return
    }

    await withPublicationUpdate(
      async () => {
        const tasks = [updateObjectModelView(token, modelObjectId, { is_published: nextPublished })]
        if (nextPublished) {
          tasks.unshift(updateObject(token, activeModelObject.id, { is_published: true }))
        }
        await Promise.all(tasks)
      },
      nextPublished ? '3D model published to Public Preview.' : '3D model hidden from Public Preview.'
    )
  }

  // Slice E: per-package "Notify followers" toggle. Stores the operator's
  // intent on the object_models row. The digest is triggered separately by
  // an admin-callable endpoint after publish + sync succeed (Slice E-4).
  async function setNotifyFollowersOnPublish(nextValue) {
    if (!modelDetail || !modelObjectId) {
      setError('Upload a model before changing notification settings.')
      return
    }
    await withPublicationUpdate(
      async () => {
        await updateObjectModelView(token, modelObjectId, { notify_followers_on_publish: nextValue })
      },
      nextValue
        ? 'Notify followers on publish: ON. The next publish + sync will fan out a digest email to subscribers.'
        : 'Notify followers on publish: OFF. No digest email will fire when this package next publishes.'
    )
  }

  async function setPublicationVideoPublished(nextPublished) {
    if (!activeModelObject || !currentModelPublicationVideo) {
      setError('Select a linked video before changing publication state.')
      return
    }

    await withPublicationUpdate(
      async () => {
        const tasks = [updateVideo(token, currentModelPublicationVideo.id, { is_published: nextPublished })]
        if (nextPublished) {
          tasks.unshift(updateObject(token, activeModelObject.id, { is_published: true }))
        }
        if (currentModelPublicationTranscript) {
          tasks.push(updateTranscript(token, currentModelPublicationTranscript.id, { is_published: nextPublished }))
        }
        await Promise.all(tasks)
      },
      nextPublished
        ? `Video${currentModelPublicationTranscript ? ' and transcript' : ''} published to Public Preview.`
        : `Video${currentModelPublicationTranscript ? ' and transcript' : ''} hidden from Public Preview.`
    )
  }

  async function setAnnotationPublished(annotationId, nextPublished) {
    const annotation = modelAnnotations.find((entry) => entry.id === annotationId)
    if (!annotation) {
      setError('Select an annotation before changing publication state.')
      return
    }

    await withPublicationUpdate(
      () => updateObjectModelAnnotation(token, annotationId, { is_published: nextPublished }),
      nextPublished ? `Published annotation "${annotation.title}".` : `Removed annotation "${annotation.title}" from Public Preview.`
    )
  }

  async function setCurrentObjectPackagePublished(nextPublished) {
    if (!activeModelObject) {
      setError('Select an object before publishing.')
      return
    }

    await withPublicationUpdate(async () => {
      const tasks = [updateObject(token, activeModelObject.id, { is_published: nextPublished })]
      if (modelDetail) {
        tasks.push(updateObjectModelView(token, activeModelObject.id, { is_published: nextPublished }))
      }
      if (currentModelPublicationVideo) {
        tasks.push(updateVideo(token, currentModelPublicationVideo.id, { is_published: nextPublished }))
      }
      if (currentModelPublicationTranscript) {
        tasks.push(updateTranscript(token, currentModelPublicationTranscript.id, { is_published: nextPublished }))
      }
      for (const annotation of modelAnnotations) {
        tasks.push(updateObjectModelAnnotation(token, annotation.id, { is_published: nextPublished }))
      }
      await Promise.all(tasks)
    }, nextPublished ? 'Published the current package to Public Preview.' : 'Removed the current package from Public Preview.')

    if (nextPublished) {
      openObjectInPublicPreview(activeModelObject.id, {
        projectId: activeModelObject.project_id,
        videoId: currentModelPublicationVideo?.id || '',
        seekMs: 0,
        forceOpen: true
      })
    }
  }

  async function handleStartLiveDatabaseSync() {
    if (!token) {
      return
    }

    setLiveDatabaseSyncRequestBusy(true)
    setError('')
    setNotice('')
    try {
      const payload = await startLiveDatabaseSync(token)
      setLiveDatabaseSyncStatus(payload)
      if (payload?.detail) {
        setNotice(payload.detail)
      }
    } catch (err) {
      setError(err.message)
      setLiveDatabaseSyncRequestBusy(false)
    }
  }

  async function executeSegmentSearch(page = 1, { queryOverride } = {}) {
    const activeQuery = (queryOverride ?? searchForm.query ?? '').trim()
    if (!activeQuery) {
      setError('Enter a query to search transcript segments.')
      return
    }
    const publishedOnly = mode === 'publicPreview'
    const pageSize = publishedOnly ? PUBLIC_PREVIEW_SEARCH_PAGE_SIZE : SEARCH_PAGE_SIZE
    const resultWindow = publishedOnly ? PUBLIC_PREVIEW_RESULT_WINDOW : undefined
    const retrievalMode = publishedOnly
      ? defaultSearchRetrievalMode(true)
      : (searchForm.retrieval_mode || defaultSearchRetrievalMode())
    setSearchLoading(true)
    setError('')
    setNotice('')
    if (!publishedOnly) {
      setSearchFeedbackBySegmentId({})
      setSearchFeedbackBusyBySegmentId({})
      setAnalysisFocusResult(null)
    }
    try {
      const requestBody = {
        query: activeQuery,
        project_id: searchForm.project_id || null,
        video_id: searchForm.video_id || null,
        published_only: publishedOnly,
        retrieval_mode: retrievalMode,
        page,
        page_size: pageSize,
        limit: resultWindow
      }
      const payload = IS_PUBLIC_APP
        ? await searchPublicSegments(requestBody)
        : await searchSegments(token, requestBody)
      setSearchResults(payload.results || [])
      setSearchMeta({
        totalResults: payload.total_results || 0,
        page: payload.page || 1,
        pageSize: payload.page_size || pageSize,
        totalPages: payload.total_pages || 0,
        resultWindow: payload.result_window || resultWindow || 100,
        resultWindowCapped: Boolean(payload.result_window_capped),
        searchLogId: payload.search_log_id || '',
        retrievalMode: payload.retrieval_mode || retrievalMode
      })
      if (!payload.results?.length) {
        setNotice('No segment matches found for this query.')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setSearchLoading(false)
    }
  }

  async function handleAnalysisSearchFeedback(result, rankPosition, isRelevant) {
    if (!searchMeta.searchLogId) {
      setError('Run the search again before leaving result feedback.')
      return
    }

    setSearchFeedbackBusyBySegmentId((prev) => ({ ...prev, [result.segment_id]: true }))
    setError('')
    try {
      await submitSearchResultFeedback(token, {
        search_query_log_id: searchMeta.searchLogId,
        segment_id: result.segment_id,
        is_relevant: isRelevant,
        lexical_match: Boolean(result.lexical_match),
        semantic_score: result.semantic_score,
        rank_score: result.rank_score,
        rank_position: rankPosition
      })
      setSearchFeedbackBySegmentId((prev) => ({
        ...prev,
        [result.segment_id]: isRelevant ? 'positive' : 'negative'
      }))
    } catch (err) {
      setError(err.message)
    } finally {
      setSearchFeedbackBusyBySegmentId((prev) => ({ ...prev, [result.segment_id]: false }))
    }
  }

  async function handleCreateTuningReport() {
    if (!token) return
    setTuningBusy(true)
    setError('')
    try {
      const report = await createSearchTuningReport(token)
      setTuningSummary((prev) => (
        prev
          ? { ...prev, latest_report: report }
          : { live_window: null, latest_report: report }
      ))
      await loadTuningSummary(token)
      setNotice('Created a new tuning report from the current feedback window.')
    } catch (err) {
      setError(err.message)
    } finally {
      setTuningBusy(false)
    }
  }

  async function handleApproveTuningReport(reportId) {
    if (!token || !reportId) return
    setTuningBusy(true)
    setError('')
    try {
      const report = await approveSearchTuningReport(token, reportId)
      setTuningSummary((prev) => (
        prev
          ? { ...prev, latest_report: report }
          : { live_window: null, latest_report: report }
      ))
      setNotice('Approved the latest tuning report for limited live comparison.')
    } catch (err) {
      setError(err.message)
    } finally {
      setTuningBusy(false)
    }
  }

  async function runSegmentSearch(event) {
    event.preventDefault()
    const queryOverride = event && typeof event.overrideQuery === 'string' ? event.overrideQuery : undefined
    await executeSegmentSearch(1, { queryOverride })
  }

  async function changeSearchPage(nextPage) {
    if (searchLoading || nextPage < 1 || (searchMeta.totalPages && nextPage > searchMeta.totalPages)) {
      return
    }
    await executeSegmentSearch(nextPage)
  }

  function clearActiveSearchResults() {
    const retrievalMode = isPublicPreviewMode ? defaultSearchRetrievalMode(true) : (searchMeta.retrievalMode || defaultSearchRetrievalMode())
    setSearchForm(emptySearchForm(retrievalMode))
    setSearchResults([])
    setSearchLoading(false)
    if (!isPublicPreviewMode) {
      setSearchFeedbackBySegmentId({})
      setSearchFeedbackBusyBySegmentId({})
      setAnalysisFocusResult(null)
    }
    setSearchMeta(
      isPublicPreviewMode
        ? initialSearchMeta(PUBLIC_PREVIEW_SEARCH_PAGE_SIZE, PUBLIC_PREVIEW_RESULT_WINDOW, defaultSearchRetrievalMode(true))
        : initialSearchMeta(SEARCH_PAGE_SIZE, 100, retrievalMode)
    )
  }

  function openSearchResult(result) {
    setMode('analysis')
    setAnalysisFocusResult(result)
    if (result.video_id !== viewerVideoId) {
      setViewerMediaUrl('')
    }
    setViewerVideoId(result.video_id)
    setPendingPlayback({ videoId: result.video_id, seekMs: result.start_ms, autoplay: true })
    setManualClip((prev) => ({ ...prev, start_ms: String(result.start_ms) }))
    setNotice(`Loaded ${result.video_title} at ${formatClock(result.start_ms)} and started playback.`)
  }

  function openObjectInPublicPreview(objectId, options = {}) {
    const {
      projectId = '',
      videoId = '',
      seekMs = 0,
      annotationId = '',
      autoplay = false,
      scrollViewerIntoView = false,
      clipPlayback = null,
      searchMoment = null,
      objectRows = sortedObjects,
      forceOpen = false
    } = options

    if (!objectId) {
      setError('Select an object before opening the public preview.')
      return
    }

    const targetObject = objectRows.find((objectRow) => objectRow.id === objectId) || null
    if (!forceOpen && !IS_PUBLIC_APP && !targetObject?.is_published) {
      setError('Publish the object before opening Public Preview.')
      return
    }

    if (projectId) {
      setModelProjectId(projectId)
    }

    const resolvedVideoId = clipPlayback?.videoId || videoId || ''
    const normalizedSeekMs = Math.max(
      0,
      Math.floor(Number.isFinite(clipPlayback?.startMs) ? clipPlayback.startMs : seekMs || 0)
    )
    const normalizedClipPlayback = clipPlayback && resolvedVideoId
      ? {
          videoId: resolvedVideoId,
          startMs: normalizedSeekMs,
          endMs: Math.max(normalizedSeekMs, Math.floor(Number(clipPlayback.endMs ?? normalizedSeekMs)))
        }
      : null

    setModelObjectId(objectId)
    setPublicPreviewVideoId(resolvedVideoId)
    setPublicPreviewAnnotationId(annotationId || '')
    setPublicPreviewClipIndex(0)
    setPublicPreviewClipPlayback(normalizedClipPlayback)
    setPublicPreviewPendingClipCommit(null)
    setPublicPreviewSearchMoment(searchMoment)
    setPublicPreviewCurrentMs(normalizedSeekMs)
    setPublicPreviewPendingPlayback(
      resolvedVideoId
        ? {
            videoId: resolvedVideoId,
            seekMs: normalizedSeekMs,
            autoplay
          }
        : null
    )
    setPublicPreviewShouldScrollViewerIntoView(Boolean(scrollViewerIntoView))
    setMode('publicPreview')
  }

  function openPublicPreviewResult(result) {
    if (IS_PUBLIC_APP) {
      const { targetUrl, state } = buildSearchResultEvidenceNavigation(result)
      if (!targetUrl || typeof window === 'undefined') {
        setError('This published result does not yet resolve to a canonical evidence page.')
        return
      }

      navigateToUrl(targetUrl, { state })
      return
    }

    const matchedVideo = sortedVideos.find((video) => video.id === result.video_id)
    if (!matchedVideo) {
      setError('The selected search result could not be resolved to a linked video.')
      return
    }
    if (!matchedVideo.object_id) {
      setError('This search result is not linked to an object yet, so public preview is unavailable.')
      return
    }
    const matchedObject = sortedObjects.find((objectRow) => objectRow.id === matchedVideo.object_id) || null

    const contextWindow = surroundingContextRange(result)
    const resultStartMs = Math.max(0, Math.floor(Number(contextWindow.contextStart || result.start_ms || 0)))
    const resultEndMs = Math.max(resultStartMs, Math.floor(Number(contextWindow.contextEnd ?? result.end_ms ?? result.start_ms ?? 0)))
    const momentStartMs = Math.max(0, Math.floor(Number(contextWindow.momentStart || result.start_ms || 0)))
    const momentEndMs = Math.max(momentStartMs, Math.floor(Number(contextWindow.momentEnd ?? result.end_ms ?? result.start_ms ?? 0)))

    openObjectInPublicPreview(matchedVideo.object_id, {
      projectId: matchedVideo.project_id,
      videoId: matchedVideo.id,
      seekMs: resultStartMs,
      autoplay: true,
      scrollViewerIntoView: true,
      clipPlayback: {
        videoId: matchedVideo.id,
        startMs: resultStartMs,
        endMs: resultEndMs
      },
      searchMoment: {
        segmentId: result.segment_id || '',
        objectId: matchedVideo.object_id,
        videoId: matchedVideo.id,
        startMs: resultStartMs,
        endMs: resultEndMs,
        matchedStartMs: momentStartMs,
        matchedEndMs: momentEndMs,
        contextStartMs: resultStartMs,
        contextEndMs: resultEndMs,
        title: result.object_name || matchedObject?.name || result.video_title || matchedVideo.title || 'Search result',
        description: result.text || '',
        videoTitle: result.video_title || matchedVideo.title || '',
        objectName: result.object_name || matchedObject?.name || '',
        projectName: result.project_name || '',
        sceneDescription: result.scene_description || '',
        thumbnailUrl: result.thumbnail_url || ''
      }
    })
    setNotice(`Opened ${(matchedObject?.name || result.object_name || 'the selected object')} at ${formatClock(resultStartMs)}.`)
  }

  const searchResultsStart = searchMeta.totalResults
    ? ((searchMeta.page - 1) * searchMeta.pageSize) + 1
    : 0
  const searchResultsEnd = searchMeta.totalResults
    ? searchResultsStart + searchResults.length - 1
    : 0
  const searchResultsSummary = searchMeta.totalResults
    ? searchMeta.resultWindowCapped
      ? `Showing ${searchResultsStart}-${searchResultsEnd} of top ${searchMeta.resultWindow} ranked results`
      : `Showing ${searchResultsStart}-${searchResultsEnd} of ${searchMeta.totalResults} results`
    : ''

  function renderSearchPagination(location = 'top') {
    if (searchMeta.totalPages <= 1) {
      return null
    }

    return (
      <div className={`search-pagination search-pagination-${location}`.trim()} aria-label="Search results pages">
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

  function beginAnnotationFromSearchResult(result) {
    openSearchResult(result)
    setManualClip({ start_ms: String(result.start_ms), end_ms: String(result.end_ms) })
    setAnnotationWorkflowSeed({
      videoId: result.video_id,
      startMs: result.start_ms,
      endMs: result.end_ms,
      title: '',
      description: result.text
    })
    setTimeout(() => {
      analysisViewerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 120)
    setNotice('Annotation workflow staged. Review the video/range, adjust start and end if needed, then use Map annotation.')
  }

  async function createClipForRange(videoId, startMs, endMs, successLabel) {
    if (!videoId) {
      setError('Select a video before creating clips.')
      return
    }
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      setError('Clip range is invalid. End timestamp must be greater than start.')
      return
    }

    setClipBusy(true)
    setError('')
    try {
      const result = await createClip(token, {
        video_id: videoId,
        start_ms: Math.max(0, Math.floor(startMs)),
        end_ms: Math.max(0, Math.floor(endMs))
      })
      await refreshClips(token)
      setNotice(`${successLabel} Export job: ${result.export_job_id || 'queue unavailable'}.`)
    } catch (err) {
      setError(err.message)
    } finally {
      setClipBusy(false)
    }
  }

  function setManualClipBoundary(which) {
    const current = Math.max(0, Math.floor(viewerCurrentMs))
    setManualClip((prev) => ({ ...prev, [which]: String(current) }))
  }

  async function createManualClip() {
    const start = Number(manualClip.start_ms)
    const end = Number(manualClip.end_ms)
    await createClipForRange(viewerVideoId, start, end, 'Manual clip queued.')
  }

  function mapCurrentRangeToAnnotation() {
    if (!viewerVideoId) {
      setError('Select a video before mapping a 3D annotation.')
      return
    }

    const currentVideo = sortedVideos.find((video) => video.id === viewerVideoId)
    if (!currentVideo) {
      setError('The selected video could not be resolved for 3D annotation mapping.')
      return
    }
    if (!currentVideo.object_id) {
      setError('Link this video to an object before creating a 3D annotation.')
      return
    }

    const startMs = Number(manualClip.start_ms)
    const endMs = Number(manualClip.end_ms)
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      setError('Set a valid start/end range before mapping a 3D annotation.')
      return
    }

    setPendingAnnotationContext({
      projectId: currentVideo.project_id,
      objectId: currentVideo.object_id,
      videoId: currentVideo.id,
      startMs: Math.floor(startMs),
      endMs: Math.floor(endMs),
      title: '',
      description: annotationWorkflowSeed?.videoId === currentVideo.id ? annotationWorkflowSeed.description : ''
    })
    setMode('model3d')
  }

  function openClipAsset(url) {
    window.open(url, '_blank', 'noopener,noreferrer')
  }

  function scrollToClipExport() {
    clipExportAnchorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  async function handleTranscriptFileImport(file) {
    if (!file) return
    const text = await file.text()
    const ext = file.name.toLowerCase().split('.').pop() || ''
    const format = ext === 'vtt' ? 'VTT' : ext === 'srt' ? 'SRT' : 'PLAIN'
    const title = file.name.replace(/\.[^.]+$/, '')
    setTranscriptForm((prev) => ({ ...prev, title, format, raw_text: cleanImportedTranscriptText(text, format) }))
    setNotice(`Loaded transcript file ${file.name} into the ingest editor.`)
  }

  function jumpToSegment(segment) {
    if (viewerBlocked || !videoRef.current) return
    const media = videoRef.current
    const requestedMs = Math.max(0, Number(segment.start_ms || 0))
    const durationMs = Number.isFinite(media.duration) ? Math.floor(media.duration * 1000) : null
    const clampedMs = durationMs && durationMs > 250 ? Math.min(requestedMs, durationMs - 250) : requestedMs

    media.currentTime = clampedMs / 1000
    setViewerCurrentMs(clampedMs)
    setManualClip((prev) => ({ ...prev, start_ms: String(clampedMs) }))
    media.play().catch(() => null)
  }

  function jumpToPublicPreviewMs(targetMs, autoplay = false, { preserveClipPlayback = false } = {}) {
    if (!publicPreviewVideo) {
      return
    }

    const nextMs = Math.max(0, Math.floor(targetMs || 0))
    if (!preserveClipPlayback) {
      setPublicPreviewClipPlayback(null)
      setPublicPreviewSearchMoment(null)
    }
    const media = publicPreviewVideoRef.current
    if (!media || media.readyState < 1) {
      setPublicPreviewPendingPlayback({
        videoId: publicPreviewVideo.id,
        seekMs: nextMs,
        autoplay
      })
      setPublicPreviewCurrentMs(nextMs)
      return
    }

    const durationMs = Number.isFinite(media.duration) ? Math.floor(media.duration * 1000) : null
    const clampedMs = durationMs && durationMs > 250 ? Math.min(nextMs, durationMs - 250) : nextMs
    media.currentTime = clampedMs / 1000
    setPublicPreviewCurrentMs(clampedMs)
    if (autoplay) {
      media.play().catch(() => null)
    } else {
      media.pause()
    }
  }

  function playPublicPreviewAnnotationClip(annotation, clipIndex = 0) {
    if (!annotation) {
      return
    }

    setPublicPreviewSearchMoment(null)

    const playlist = normalizeAnnotationPlaylist(annotation)
    const clip = playlist[clipIndex] || null
    const startMs = Math.max(0, Math.floor(clip?.start_ms ?? annotation.start_ms ?? 0))
    const endMs = Math.max(startMs, Math.floor(clip?.end_ms ?? annotation.end_ms ?? startMs))
    const videoId = clip?.video_id || annotation.video_id
    const nextClipPlayback = {
      videoId,
      startMs,
      endMs
    }

    if (videoId && videoId !== publicPreviewVideo?.id) {
      setPublicPreviewPendingClipCommit(null)
      setPublicPreviewAnnotationId(annotation.id)
      setPublicPreviewClipIndex(clipIndex)
      setPublicPreviewClipPlayback(nextClipPlayback)
      setPublicPreviewVideoId(videoId)
      setPublicPreviewPendingPlayback({
        videoId,
        seekMs: startMs,
        autoplay: true
      })
      setPublicPreviewCurrentMs(startMs)
      return
    }

    if (publicPreviewVideo?.id === videoId && publicPreviewAnnotationId && publicPreviewAnnotationId === annotation.id && publicPreviewClipIndex !== clipIndex) {
      setPublicPreviewPendingClipCommit({
        annotationId: annotation.id,
        clipIndex,
        videoId,
        clipPlayback: nextClipPlayback
      })
    } else {
      setPublicPreviewPendingClipCommit(null)
      setPublicPreviewAnnotationId(annotation.id)
      setPublicPreviewClipIndex(clipIndex)
      setPublicPreviewClipPlayback(nextClipPlayback)
    }

    setPublicPreviewPendingPlayback({
      videoId,
      seekMs: startMs,
      autoplay: true
    })
    setPublicPreviewCurrentMs(startMs)
  }

  function focusPublicPreviewAnnotation(annotation) {
    if (!annotation) {
      return
    }

    setPublicPreviewPendingClipCommit(null)
    setPublicPreviewSearchMoment(null)
    setPublicPreviewAnnotationId(annotation.id)
    setPublicPreviewClipIndex(0)
  }

  function buildPublicPreviewEvidenceNavigation(annotation = null, { preferMoment = false } = {}) {
    const annotationEvidenceUrl = normalizeEvidenceUrl(annotation?.evidence_url)
    const objectEvidenceUrl = publicPreviewObjectEvidenceUrl
    const baseEvidenceUrl = objectEvidenceUrl || annotationEvidenceUrl
    const currentSeekMs = Number.isFinite(publicPreviewCurrentMs) ? Math.max(0, Math.floor(publicPreviewCurrentMs)) : 0
    const currentFocusStartMs = Number.isFinite(publicPreviewClip?.start_ms)
      ? Math.max(0, Math.floor(publicPreviewClip.start_ms))
      : Math.max(0, Math.floor(annotation?.start_ms || 0))
    const annotationContextActive = Boolean(
      annotationEvidenceUrl
      && msWithinWindow(currentSeekMs, Number(annotation?.start_ms || 0), Number(annotation?.end_ms || annotation?.start_ms || 0))
    )
    const canUseCanonicalAnnotationUrl = Boolean(
      annotationEvidenceUrl
      && annotationContextActive
      && publicPreviewClipIndex === 0
      && approximatelySameMoment(currentSeekMs, currentFocusStartMs)
    )
    const canUseExactMomentUrl = Boolean(baseEvidenceUrl && publicPreviewCurrentStableVideoId)

    let targetUrl = annotationEvidenceUrl || objectEvidenceUrl
    if (preferMoment && canUseExactMomentUrl && !canUseCanonicalAnnotationUrl) {
      targetUrl = buildEvidenceVideoMomentUrl(baseEvidenceUrl, publicPreviewCurrentStableVideoId, currentSeekMs)
    } else if (!targetUrl && canUseExactMomentUrl) {
      targetUrl = buildEvidenceVideoMomentUrl(baseEvidenceUrl, publicPreviewCurrentStableVideoId, currentSeekMs)
    }

    const websiteObjectId = evidenceUrlObjectId(targetUrl || baseEvidenceUrl)
    if (!targetUrl || !websiteObjectId) {
      return { targetUrl: '', state: null }
    }

    const annotationPublicId = annotationContextActive
      ? evidenceUrlQueryValue(annotationEvidenceUrl, 'annotation')
      : ''

    return {
      targetUrl,
      state: {
        semanticEvidenceHandoff: {
          websiteObjectId,
          annotationId: annotationPublicId,
          videoId: publicPreviewCurrentStableVideoId,
          seekMs: currentSeekMs,
          autoplay: true,
          source: annotationPublicId ? 'annotation' : (canUseExactMomentUrl ? 'video' : 'default')
        }
      }
    }
  }

  function openPublicPreviewEvidencePage() {
    const { targetUrl, state } = buildPublicPreviewEvidenceNavigation(publicPreviewSelectedAnnotation, {
      preferMoment: true
    })
    if (!targetUrl || typeof window === 'undefined') {
      return false
    }

    return navigateToUrl(targetUrl, { state })
  }

  function openPublicPreviewAnnotationEvidence(annotation) {
    const { targetUrl, state } = buildPublicPreviewEvidenceNavigation(annotation || publicPreviewSelectedAnnotation, {
      preferMoment: true
    })
    if (!targetUrl || typeof window === 'undefined') {
      return false
    }

    return navigateToUrl(targetUrl, { state })
  }

  function finishPublicPreviewClip(options = {}) {
    const { autoAdvance = true } = options
    const media = publicPreviewVideoRef.current
    if (!media) {
      return
    }

    if (!publicPreviewSelectedAnnotation || !publicPreviewClip) {
      if (!publicPreviewClipPlayback) {
        return
      }

      const clipStartMs = Math.max(0, Number(publicPreviewClipPlayback.startMs || 0))
      media.pause()
      media.currentTime = clipStartMs / 1000
      setPublicPreviewCurrentMs(clipStartMs)
      return
    }

    if (autoAdvance && publicPreviewClipIndex + 1 < publicPreviewSelectedAnnotationPlaylist.length) {
      playPublicPreviewAnnotationClip(publicPreviewSelectedAnnotation, publicPreviewClipIndex + 1)
      return
    }

    const clipStartMs = Math.max(0, Number(publicPreviewClip.start_ms || 0))
    media.pause()
    media.currentTime = clipStartMs / 1000
    setPublicPreviewCurrentMs(clipStartMs)
  }

  function selectPublicPreviewAnnotation(annotation) {
    playPublicPreviewAnnotationClip(annotation, 0)
  }

  function undoViewerDraft() {
    if (viewerHistory.index < 1) return
    const previous = viewerHistory.entries[viewerHistory.index - 1] || ''
    setViewerHistory((prev) => ({ ...prev, index: prev.index - 1 }))
    setViewerDraftText(previous)
  }

  async function saveViewerTranscript() {
    if (!viewerVideoId || !viewerTranscript) return
    setViewerSaving(true)
    setError('')
    try {
      const result = await ingestTranscript(token, {
        video_id: viewerVideoId,
        title: viewerTranscript.title,
        format: viewerTranscript.format,
        language: viewerTranscript.language,
        raw_text: viewerDraftText
      })
      setViewerTranscript(result.transcript)
      const refreshed = await getVideoTranscript(token, viewerVideoId)
      setViewerSegments(refreshed.segments || [])
      setViewerDraftText(refreshed.transcript.raw_text)
      pushUndoState(refreshed.transcript.raw_text)
      setNotice('Transcript saved and reindexed for analysis.')
    } catch (err) {
      setError(err.message)
    } finally {
      setViewerSaving(false)
    }
  }

  async function retryViewerTranscode() {
    if (!activeVideo) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await queueVideoTranscode(token, activeVideo.id)
      await refreshData(token)
      if (result.queued) {
        setNotice('Transcode re-queued. Playback and seek controls will unlock at READY.')
      } else {
        setError('Transcode queue is currently unavailable. Please retry in a moment.')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function backfillProjectTranscodes() {
    if (!activeVideo?.project_id) {
      setError('Select a video first so backfill can target its project.')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await backfillTranscodes(token, activeVideo.project_id)
      await refreshData(token)
      setNotice(
        `Backfill queued: ${result.queued}, already ready: ${result.skipped_ready}, queue failures: ${result.failed_queue}.`
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function removeClip(clipId) {
    if (!window.confirm('Remove this clip and its exported files?')) {
      return
    }

    setClipBusy(true)
    setError('')
    try {
      await deleteClip(token, clipId)
      await refreshClips(token)
      setNotice('Clip removed.')
    } catch (err) {
      setError(err.message)
    } finally {
      setClipBusy(false)
    }
  }

  async function removeVideo(videoId) {
    if (!window.confirm('Delete this video record? This also removes linked transcripts, clips, and 3D annotation media references.')) {
      return
    }

    setBusy(true)
    setError('')
    try {
      const remainingVideos = sortedVideos.filter((video) => video.id !== videoId)
      await deleteVideo(token, videoId)
      await refreshData(token)
      await refreshClips(token)
      if (viewerVideoId === videoId) {
        setViewerVideoId(remainingVideos[0]?.id || '')
      }
      if (videoEdit.id === videoId) {
        setVideoEdit({ id: '', title: '', object_id: '' })
      }
      if (transcriptForm.video_id === videoId) {
        setTranscriptForm((prev) => ({
          ...prev,
          video_id: remainingVideos[0]?.id || '',
          title: remainingVideos[0] ? `${remainingVideos[0].title} Transcript` : prev.title
        }))
      }
      setNotice('Video removed.')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    const currentSurfaceTitle = !token && !IS_PUBLIC_APP
      ? (isPreviewRouteLocked ? 'Preview Sign In' : 'Sign In')
      : IS_PUBLIC_APP
        ? 'Live Database'
        : isPreviewRouteLocked
          ? 'Public Preview'
          : mode === 'tuning'
            ? 'Tuning Review'
            : mode === 'ops'
              ? 'Content Upload'
              : mode === 'model3d'
                ? '3D Review'
                : mode === 'publicPreview'
                  ? 'Public Preview'
                  : 'Research Analysis'

    document.title = formatLociTitle(currentSurfaceTitle)
  }, [isPreviewRouteLocked, mode, token])

  if (!token && !IS_PUBLIC_APP) {
    return (
      <main
        className={`login-screen ${isPrivateAuthoringSurface ? 'authoring-surface' : ''}`.trim()}
        data-studio-theme={isPrivateAuthoringSurface ? palette : undefined}
      >
        <div className="orb one" />
        <div className="orb two" />
        <form className="login-card" onSubmit={handleLogin}>
          <div className="login-branding">
            <BrandLockup variant="stacked" />
            <p className="brand-tagline">Evidence, located.</p>
          </div>
          {isPrivateAuthoringSurface ? (
            <div className="authoring-login-context">
              <span className="authoring-context-label">Private authoring</span>
              <StudioThemeToggle palette={palette} onToggle={toggleStudioTheme} />
            </div>
          ) : null}
          <p className="kicker">{isPreviewRouteLocked ? 'Loci Preview' : 'Loci Console'}</p>
          <h1>Sign in</h1>
          <p className="muted">
            {isPreviewRouteLocked
              ? 'Authenticated preview surface for the published mobile shell. This route bypasses the operator console.'
              : 'Private Loci operator console for ingest, analysis, and 3D review.'}
          </p>
          <label>
            Email
            <input value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button disabled={busy} type="submit">
            {busy ? 'Signing in...' : isPreviewRouteLocked ? 'Open Preview' : 'Open Loci Console'}
          </button>
          {error ? <p className="error">{error}</p> : null}
        </form>
      </main>
    )
  }

  const consoleShellTitle = IS_PUBLIC_APP ? 'Live Database' : (isPreviewRouteLocked ? 'Public Preview' : 'Research Analysis')

  const publicPreviewShell = (
    <PublicExperienceShell
      isPublicApp={IS_PUBLIC_APP}
      publicSurfaceTitle={publicSurfaceTitle}
      modelProjectId={modelProjectId}
      setModelProjectId={setModelProjectId}
      modelObjectId={modelObjectId}
      setModelObjectId={setModelObjectId}
      publishedProjects={publishedProjects}
      filteredPublishedObjectsForPreview={filteredPublishedObjectsForPreview}
      openNowObjectIds={openNowObjectIds}
      publicPreviewLoading={publicPreviewLoading}
      publicPreviewData={publicPreviewData}
      publicPreviewVideoId={publicPreviewVideoId}
      setPublicPreviewVideoId={setPublicPreviewVideoId}
      setPublicPreviewClipPlayback={setPublicPreviewClipPlayback}
      setPublicPreviewPendingPlayback={setPublicPreviewPendingPlayback}
      setPublicPreviewCurrentMs={setPublicPreviewCurrentMs}
      publicPreviewCurrentMs={publicPreviewCurrentMs}
      publicPreviewViewerRef={publicPreviewViewerRef}
      publicPreviewVideoRef={publicPreviewVideoRef}
      applyPublicPreviewPendingPlayback={applyPublicPreviewPendingPlayback}
      publicPreviewClipPlayback={publicPreviewClipPlayback}
      publicPreviewSearchMoment={publicPreviewSearchMoment}
      finishPublicPreviewClip={finishPublicPreviewClip}
      publicPreviewSelectedAnnotationPlaylist={publicPreviewSelectedAnnotationPlaylist}
      publicPreviewClipIndex={publicPreviewClipIndex}
      playPublicPreviewAnnotationClip={playPublicPreviewAnnotationClip}
      publicPreviewTranscriptListRef={publicPreviewTranscriptListRef}
      publicPreviewSegments={publicPreviewSegments}
      publicPreviewCurrentSegmentIndex={publicPreviewCurrentSegmentIndex}
      jumpToPublicPreviewMs={jumpToPublicPreviewMs}
      token={token}
      getObjectModelFileUrl={getObjectModelFileUrl}
      publicPreviewModelTransform={publicPreviewModelTransform}
      publicPreviewCameraView={publicPreviewCameraView}
      publicPreviewSelectedAnnotation={publicPreviewSelectedAnnotation}
      publicPreviewClipVideo={publicPreviewClipVideo}
      publicPreviewClip={publicPreviewClip}
      focusPublicPreviewAnnotation={focusPublicPreviewAnnotation}
      openPublicPreviewEvidencePage={openPublicPreviewEvidencePage}
      openPublicPreviewAnnotationEvidence={openPublicPreviewAnnotationEvidence}
      selectPublicPreviewAnnotation={selectPublicPreviewAnnotation}
      searchForm={searchForm}
      setSearchForm={setSearchForm}
      runSegmentSearch={runSegmentSearch}
      publishedVideos={publishedVideos}
      searchLoading={searchLoading}
      searchResults={searchResults}
      searchResultsSummary={searchResultsSummary}
      renderSearchPagination={renderSearchPagination}
      clearActiveSearchResults={clearActiveSearchResults}
      semanticFitTier={semanticFitTier}
      formatSemanticFit={formatSemanticFit}
      formatClock={formatClock}
      sortedVideos={sortedVideos}
      publishedObjectIds={publishedObjectIds}
      openPublicPreviewResult={openPublicPreviewResult}
      setPublicPreviewSearchMoment={setPublicPreviewSearchMoment}
      focusEvidenceSignal={publicPreviewShouldScrollViewerIntoView}
      EmptyState={EmptyState}
    />
  )

  return (
    <main
      className={`app-shell ${isPreviewRouteLocked ? 'preview-route' : ''} ${isPrivateAuthoringSurface ? 'authoring-surface' : ''}`.trim()}
      data-studio-theme={isPrivateAuthoringSurface ? palette : undefined}
    >
      {mediaUploading ? (
        <div className="overlay-backdrop" role="status" aria-live="polite">
          <div className="overlay-card">
            <strong>Uploading media...</strong>
            <p className="muted">Transferring the file to media inventory. Registration starts after upload completes.</p>
            <div className="upload-progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={mediaUploadProgress}>
              <div className="upload-progress-fill" style={{ width: `${mediaUploadProgress}%` }} />
              <span>{mediaUploadProgress}%</span>
            </div>
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        open={Boolean(confirmAction)}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmLabel={confirmAction?.confirmLabel || 'Confirm'}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          const action = confirmAction
          setConfirmAction(null)
          action?.onConfirm?.()
        }}
      />

      <header className="topbar">
        <div className="topbar-branding">
          {IS_PUBLIC_APP ? (
            // On the IS_PUBLIC_APP "Live Database" surface (post Slice F deploy
            // this surface lives at /preview behind basic_auth; pre-deploy it
            // also serves at /), the lockup must be a back-to-home affordance.
            // Pattern mirrors apps/web/src/public/PublicBrowseApp.jsx:513-524
            // and apps/web/src/evidence/EvidenceObjectApp.jsx:1952-1963.
            // Closes the paired sub-item under Future Enhancement Candidate 2
            // tracked in the canonical plan as of 2026-04-23.
            <a
              className="surface-branding surface-branding-link"
              href={PUBLIC_BROWSE_PATH}
              aria-label="Return to the Loci public homepage"
              onClick={(event) => {
                event.preventDefault()
                navigateToPublicBrowse()
              }}
            >
              <BrandLockup variant="horizontal" />
            </a>
          ) : (
            <BrandLockup variant="horizontal" />
          )}
          <div className="topbar-copy">
            {isPrivateAuthoringSurface ? <span className="authoring-context-label">Private authoring</span> : null}
            <p className="kicker">{isPreviewRouteLocked ? 'Loci Preview' : 'Loci Console'}</p>
            <h1>{consoleShellTitle}</h1>
            {isPreviewRouteLocked ? <p className="muted preview-route-topbar-note">Authenticated published-object preview only.</p> : null}
          </div>
        </div>
        {!IS_PUBLIC_APP ? (
          <div className="topbar-actions">
            {!isPreviewRouteLocked ? <StudioThemeToggle palette={palette} onToggle={toggleStudioTheme} /> : null}
            {!isPreviewRouteLocked ? (
              <>
                <button className={mode === 'tuning' ? '' : 'ghost'} onClick={() => setMode('tuning')}>
                  Tuning
                </button>
                <button className={mode === 'ops' ? '' : 'ghost'} onClick={() => setMode('ops')}>
                  Content Upload
                </button>
                <button className={mode === 'analysis' ? '' : 'ghost'} onClick={() => setMode('analysis')}>
                  Analysis
                </button>
                <button className={mode === 'model3d' ? '' : 'ghost'} onClick={() => setMode('model3d')}>
                  3D Model
                </button>
                <button className={mode === 'publicPreview' ? '' : 'ghost'} onClick={() => setMode('publicPreview')}>
                  Public Preview
                </button>
              </>
            ) : null}
            <button className="ghost" onClick={() => window.location.reload()}>
              Refresh
            </button>
            <button className="ghost" onClick={handleLogout}>
              Logout
            </button>
          </div>
        ) : (
          // IS_PUBLIC_APP topbar has no mode-buttons (the "Live Database"
          // surface is read-only spot-check), so the actions slot becomes
          // a single "Return to homepage" anchor. Belt-and-braces with the
          // wrapped lockup above so a keyboard-only user has two paths home.
          <div className="topbar-actions">
            <a
              className="button-link secondary"
              href={PUBLIC_BROWSE_PATH}
              onClick={(event) => {
                event.preventDefault()
                navigateToPublicBrowse()
              }}
            >
              Return to homepage
            </a>
          </div>
        )}
      </header>

      {notice ? <p className="notice">{notice}</p> : null}
      {error ? <p className="error">{error}</p> : null}

      {mode === 'analysis' ? (
        <section className="grid one">
          <Card title="Analysis" subtitle="Video, transcript, search, and clip review.">
              <div className="stack search-panel-top">
                <h3>Search</h3>
                <p className="muted">Search transcripts and scene evidence, open an interpretive moment, or export a clip.</p>
                <form className="stack" onSubmit={runSegmentSearch}>
                  <SearchAutosuggest
                    value={searchForm.query}
                    onChange={(next) => setSearchForm((prev) => ({ ...prev, query: next }))}
                    onSubmit={(phrase) => {
                      setSearchForm((prev) => ({ ...prev, query: phrase }))
                      runSegmentSearch({ preventDefault() {}, overrideQuery: phrase })
                    }}
                    projectId={searchForm.project_id || undefined}
                    token={token}
                    placeholder="Search transcript text"
                    inputProps={{ 'aria-label': 'Analysis search query' }}
                  />
                  <div className="row analysis-search-filter-row">
                    <select
                      value={searchForm.project_id}
                      onChange={(e) => setSearchForm((prev) => ({ ...prev, project_id: e.target.value, video_id: '' }))}
                    >
                      <option value="">All projects</option>
                      {projects.map((project) => (
                        <option value={project.id} key={project.id}>
                          {project.name}
                        </option>
                      ))}
                    </select>
                    <select
                      value={searchForm.video_id}
                      onChange={(e) => setSearchForm((prev) => ({ ...prev, video_id: e.target.value }))}
                    >
                      <option value="">All videos</option>
                      {sortedVideos
                        .filter((video) => !searchForm.project_id || video.project_id === searchForm.project_id)
                        .map((video) => (
                          <option value={video.id} key={video.id}>
                            {video.title}
                          </option>
                        ))}
                    </select>
                      <select
                        aria-label="Search mode"
                        value={searchForm.retrieval_mode}
                        onChange={(e) => setSearchForm((prev) => ({ ...prev, retrieval_mode: e.target.value }))}
                      >
                        <option value="combined">Transcript + scene</option>
                        <option value="transcript_only">Transcript only</option>
                        <option value="visual_only">Visual only</option>
                      </select>
                  </div>
                  <button type="submit" disabled={searchLoading || clipBusy}>
                    {searchLoading ? 'Searching...' : 'Search'}
                  </button>
                </form>

                {searchResults.length ? (
                  <>
                    <div className="search-results-toolbar">
                      <div className="search-results-summary">
                        <strong>{searchResultsSummary}</strong>
                        <span className="search-mode-indicator">Search mode: {searchRetrievalModeLabel(searchMeta.retrievalMode)}</span>
                        <span className="search-mode-detail">{searchModeSummaryLine(searchMeta.retrievalMode)}</span>
                        {searchMeta.resultWindowCapped ? (
                          <p className="muted">Results are capped to the top-ranked {searchMeta.resultWindow}. Refine the query or filters to narrow further.</p>
                        ) : (
                          <p className="muted">Ranked results are paged in sets of {searchMeta.pageSize} so longer result lists stay scannable.</p>
                        )}
                      </div>
                      {renderSearchPagination('top')}
                    </div>
                    <ul className="list">
                    {searchResults.map((result, index) => {
                      const semanticTier = semanticFitTier(result.semantic_score)
                      const visualTier = semanticFitTier(result.visual_score)
                      const feedbackState = searchFeedbackBySegmentId[result.segment_id] || ''
                      const feedbackBusy = Boolean(searchFeedbackBusyBySegmentId[result.segment_id])
                      const rankPosition = searchResultsStart + index
                      const contextWindow = surroundingContextRange(result)
                      const resultThumbnailUrl = buildAuthenticatedAssetUrl(token, result.thumbnail_url)
                      const transcriptFitLabel = fitChipText('Transcript fit', result.semantic_score)
                      const sceneFitLabel = fitChipText('Scene fit', result.visual_score)

                      return (
                      <li
                        key={result.segment_id}
                        className={`search-result-row search-result-shell ${analysisFocusResult?.segment_id === result.segment_id ? 'active' : ''}`.trim()}
                      >
                        <div className="search-result-media">
                          {resultThumbnailUrl ? (
                            <img src={resultThumbnailUrl} alt="Representative frame for this search result" className="search-result-thumbnail" />
                          ) : (
                            <div className="search-result-thumbnail-placeholder">
                              <span>Scene evidence pending</span>
                            </div>
                          )}
                        </div>
                        <div className="list-main video-list-main search-result-body">
                          <div className="search-result-heading">
                            <div className="search-result-heading-copy">
                              <p className="search-result-kicker">{result.video_title}</p>
                              <strong>Interpretive moment {formatClock(result.start_ms)}-{formatClock(result.end_ms)}</strong>
                              <span className="search-result-context">Surrounding context {formatClock(contextWindow.contextStart)}-{formatClock(contextWindow.contextEnd)}</span>
                            </div>
                          </div>
                          <div className="search-result-evidence-group">
                            <div className="search-result-evidence-block">
                              <span className="search-result-evidence-label">Revealed line</span>
                              <p className="search-result-text">{result.text}</p>
                            </div>
                            {result.scene_description ? (
                              <div className="search-result-evidence-block">
                                <span className="search-result-evidence-label">Scene description</span>
                                <p className="search-result-scene-line">{result.scene_description}</p>
                              </div>
                            ) : null}
                          </div>
                          <p className="search-result-match-line">{searchMatchBasisLine(result, searchMeta.retrievalMode)}</p>
                        </div>
                        <div className="search-result-rail" aria-label="Match support and feedback">
                          <div className="search-result-meta">
                            {result.lexical_match ? (
                              <span className="search-fit-chip lexical">Keyword match</span>
                            ) : null}
                            {transcriptFitLabel && semanticTier ? (
                              <span
                                className={`search-fit-chip semantic ${semanticTier.tone}`}
                                title="Approximate semantic relevance score from vector similarity. Higher is closer, but it is not a probability."
                              >
                                {transcriptFitLabel}
                              </span>
                            ) : null}
                            {sceneFitLabel ? (
                              <span
                                className={`search-fit-chip visual ${visualTier?.tone || ''}`.trim()}
                                title="Approximate scene-similarity score from the visual description embedding for this transcript window."
                              >
                                {sceneFitLabel}
                              </span>
                            ) : null}
                          </div>
                          <div className="search-feedback-controls" aria-label="Search result feedback">
                            <button
                              type="button"
                              className={`ghost search-feedback-button ${feedbackState === 'positive' ? 'active-positive' : ''}`.trim()}
                              aria-label="Mark this result as useful"
                              aria-pressed={feedbackState === 'positive'}
                              title="Mark this result as useful"
                              disabled={feedbackBusy}
                              onClick={() => handleAnalysisSearchFeedback(result, rankPosition, true)}
                            >
                              <FeedbackGlyph direction="up" />
                            </button>
                            <button
                              type="button"
                              className={`ghost search-feedback-button ${feedbackState === 'negative' ? 'active-negative' : ''}`.trim()}
                              aria-label="Mark this result as weak or misleading"
                              aria-pressed={feedbackState === 'negative'}
                              title="Mark this result as weak or misleading"
                              disabled={feedbackBusy}
                              onClick={() => handleAnalysisSearchFeedback(result, rankPosition, false)}
                            >
                              <FeedbackGlyph direction="down" />
                            </button>
                          </div>
                        </div>
                        <div className="search-result-actions">
                          <button type="button" className="ghost search-result-button" onClick={() => openSearchResult(result)}>
                            Open in player
                          </button>
                          <button type="button" className="ghost search-result-button" onClick={() => beginAnnotationFromSearchResult(result)}>
                            Create annotation
                          </button>
                          <button
                            type="button"
                            className="search-result-button"
                            disabled={clipBusy}
                              onClick={() => {
                                scrollToClipExport()
                                createClipForRange(
                                  result.video_id,
                                  result.start_ms,
                                  result.end_ms,
                                  'Search-result clip queued.'
                                )
                              }}
                          >
                            Create clip
                          </button>
                        </div>
                      </li>
                    )})}
                  </ul>
                  {renderSearchPagination('bottom')}
                  </>
                ) : <EmptyState title="No results" text="Run a lexical or semantic query to load matching timestamps." />}
              </div>

            <div className="stack">
              <select value={viewerVideoId} onChange={(e) => setViewerVideoId(e.target.value)}>
                {sortedVideos.map((video) => (
                  <option value={video.id} key={video.id}>
                    {video.title} ({shortId(video.id)})
                  </option>
                ))}
              </select>
              {activeVideo ? (
                <p className="muted">
                  {activeVideo.title} · {activeVideo.status} · {activeVideo.duration_ms ? formatClock(activeVideo.duration_ms) : 'duration n/a'}
                </p>
              ) : null}
              {transcodeMeta.show ? (
                <div className={`transcode-status ${activeVideo?.status === 'FAILED' ? 'failed' : ''}`}>
                  <div className="transcode-header-row">
                    <strong>{activeVideo?.status === 'FAILED' ? 'Transcode failed' : 'Transcoding in progress'}</strong>
                    <span>{activeVideo?.status === 'FAILED' ? 'Action needed' : `${transcodeMeta.percent}%`}</span>
                  </div>
                  {activeVideo?.status !== 'FAILED' ? (
                    <>
                      <div className="transcode-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={transcodeMeta.percent}>
                        <div className="transcode-progress-fill" style={{ width: `${transcodeMeta.percent}%` }} />
                      </div>
                      <p className="muted">
                        {transcodeMeta.label}
                        {shouldShowEta(transcodeMeta.remainingMs) ? ` Estimated remaining: ${formatEta(transcodeMeta.remainingMs)}.` : ''} Auto-refresh runs every 5 seconds.
                      </p>
                    </>
                  ) : (
                    <p className="muted">Normalized playback output is unavailable. Retry transcode to continue viewer validation.</p>
                  )}
                  <div className="topbar-actions">
                    <button type="button" className="ghost" disabled={busy || activeVideo?.status === 'TRANSCODING'} onClick={retryViewerTranscode}>
                      {activeVideo?.status === 'TRANSCODING' ? 'Transcoding...' : 'Retry transcode'}
                    </button>
                    <button type="button" className="ghost" disabled={busy || !activeVideo?.project_id} onClick={backfillProjectTranscodes}>
                      Backfill project transcodes
                    </button>
                  </div>
                </div>
              ) : null}
              {viewerBlocked ? (
                <p className="muted">
                  Playback and transcript seeking unlock when this video reaches READY.
                </p>
              ) : null}
            </div>

            {viewerLoading ? <p className="muted">Loading video and transcript...</p> : null}
            {!viewerLoading && activeVideo ? (
              <div className="viewer-layout">
                <section className="viewer-video" ref={analysisViewerRef}>
                  {viewerMediaUrl ? (
                    <video
                      ref={videoRef}
                      src={viewerMediaUrl}
                      controls
                      onLoadedMetadata={() => applyPendingPlayback(videoRef.current)}
                      onTimeUpdate={(e) => setViewerCurrentMs(Math.floor((e.target.currentTime || 0) * 1000))}
                    />
                  ) : (
                    <EmptyState
                      title={viewerBlocked ? 'Playback locked' : 'Playback unavailable'}
                      text={viewerBlocked ? 'This video is still processing. Wait for READY to play and scrub.' : 'The normalized playback asset is not available yet.'}
                    />
                  )}
                </section>

                <section className="viewer-transcript">
                  <div className="viewer-transcript-header">
                    <div>
                      <h3>Transcript</h3>
                      {analysisFocusContext ? (
                        <p className="muted">
                          Revealed moment {formatClock(analysisFocusContext.momentStart)}-{formatClock(analysisFocusContext.momentEnd)} within surrounding context {formatClock(analysisFocusContext.contextStart)}-{formatClock(analysisFocusContext.contextEnd)}.
                        </p>
                      ) : (
                        <p className="muted">Open a result in the player to hold the revealed moment and surrounding context here.</p>
                      )}
                    </div>
                    {analysisFocusResult ? (
                      <button type="button" className="ghost transcript-focus-clear" onClick={() => setAnalysisFocusResult(null)}>
                        Clear focus
                      </button>
                    ) : null}
                  </div>
                  <div className={`transcript-list ${analysisFocusContext ? 'focus-active' : ''}`.trim()} ref={transcriptListRef}>
                    {viewerSegments.length ? (
                      viewerSegments.map((segment, idx) => {
                        const isFocusedMoment = analysisFocusResult?.video_id === viewerVideoId && analysisFocusResult?.segment_id === segment.id
                        const inFocusedContext = analysisFocusResult?.video_id === viewerVideoId && segmentWithinResultContext(segment, analysisFocusResult)

                        return (
                        <button
                          type="button"
                          key={segment.id}
                          data-segment-index={idx}
                          className={[
                            'transcript-line',
                            inFocusedContext ? 'in-context' : '',
                            isFocusedMoment ? 'focused-moment' : '',
                            idx === currentSegmentIndex ? 'active' : ''
                          ].join(' ').trim()}
                          disabled={viewerBlocked}
                          onClick={() => jumpToSegment(segment)}
                        >
                          <span>{formatClock(segment.start_ms)}</span>
                          <strong>{segment.text}</strong>
                        </button>
                      )})
                    ) : <EmptyState title="No transcript yet" text="Ingest transcript text for this video to enable transcript review and search context." />}
                  </div>
                </section>
              </div>
            ) : null}

            <div className="stack">
              <div ref={clipExportAnchorRef} className="clip-export-anchor" aria-hidden="true" />
              <h3>Clip Export</h3>
                <div className="result-box clip-export-panel">
                <p>
                  Current video: <strong>{activeVideo?.title || 'none selected'}</strong>
                </p>
                  <p className="muted">Set the export range in milliseconds from the start of the selected video.</p>
                  <div className="clip-range-grid">
                    <label className="field">
                      <span className="ingest-label">Start ms</span>
                      <input
                        value={manualClip.start_ms}
                        onChange={(e) => setManualClip((prev) => ({ ...prev, start_ms: e.target.value }))}
                        placeholder="0"
                      />
                    </label>
                    <label className="field">
                      <span className="ingest-label">End ms</span>
                      <input
                        value={manualClip.end_ms}
                        onChange={(e) => setManualClip((prev) => ({ ...prev, end_ms: e.target.value }))}
                        placeholder="12000"
                      />
                    </label>
                </div>
                  <div className="topbar-actions clip-export-actions">
                  <button type="button" className="ghost" onClick={() => setManualClipBoundary('start_ms')}>
                    Set start from current time
                  </button>
                  <button type="button" className="ghost" onClick={() => setManualClipBoundary('end_ms')}>
                    Set end from current time
                  </button>
                  <button type="button" className="ghost" onClick={mapCurrentRangeToAnnotation}>
                    Map to 3D
                  </button>
                  <button type="button" disabled={clipBusy} onClick={createManualClip}>
                    Export clip
                  </button>
                </div>
              </div>

              <h3>Recent Clips</h3>
              {recentClips.length ? (
                <ul className="list">
                  {recentClips.map((clip) => (
                    <li key={clip.id}>
                      <div className="list-main video-list-main">
                        <strong>{formatClock(clip.start_ms)} - {formatClock(clip.end_ms)}</strong>
                        <span>{clip.status} · {shortId(clip.id)}</span>
                      </div>
                      <div className="topbar-actions">
                        <button
                          type="button"
                          className="ghost"
                          disabled={clip.status !== 'COMPLETE'}
                          onClick={() => openClipAsset(getClipDownloadUrl(token, clip.id))}
                        >
                          MP4
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={clip.status !== 'COMPLETE'}
                          onClick={() => openClipAsset(getClipTranscriptUrl(token, clip.id))}
                        >
                          Transcript txt
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={clipBusy}
                          onClick={() => removeClip(clip.id)}
                        >
                          Remove
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState title="No clips yet" text="Export a manual range or create one from search results." />}
            </div>

            <div className="stack analysis-evidence-panel">
              <h3>Evidence context</h3>
              {analysisFocusResult ? (
                <div className="result-box analysis-evidence-box">
                  <div className="analysis-evidence-header">
                    <div className="analysis-evidence-heading">
                      <strong>{analysisFocusResult.video_title}</strong>
                      <p className="muted">{analysisFocusMatchLine}</p>
                    </div>
                    <span className="search-fit-chip lexical">{searchRetrievalModeLabel(analysisSearchMeta.retrievalMode)}</span>
                  </div>

                  <div className="analysis-evidence-metadata">
                    <div>
                      <span className="ingest-label">Revealed moment</span>
                      <strong>{formatClock(analysisFocusContext?.momentStart || analysisFocusResult.start_ms)}-{formatClock(analysisFocusContext?.momentEnd || analysisFocusResult.end_ms)}</strong>
                    </div>
                    <div>
                      <span className="ingest-label">Surrounding context</span>
                      <strong>{formatClock(analysisFocusContext?.contextStart || analysisFocusResult.start_ms)}-{formatClock(analysisFocusContext?.contextEnd || analysisFocusResult.end_ms)}</strong>
                    </div>
                    <div>
                      <span className="ingest-label">Match basis</span>
                      <strong>{searchMatchBasisLine(analysisFocusResult, analysisSearchMeta.retrievalMode)}</strong>
                    </div>
                    <div>
                      <span className="ingest-label">Transcript fit</span>
                      <strong>{fitChipText('', analysisFocusResult.semantic_score)?.trim() || 'Not scored'}</strong>
                    </div>
                    <div>
                      <span className="ingest-label">Scene fit</span>
                      <strong>{fitChipText('', analysisFocusResult.visual_score)?.trim() || 'Not scored'}</strong>
                    </div>
                  </div>

                  <div className="analysis-evidence-body">
                    <div className="analysis-evidence-copy">
                      <div className="analysis-evidence-block">
                        <span className="ingest-label">Scene description</span>
                        <p>{analysisFocusResult.scene_description || 'No scene description is stored for this result yet. Use the transcript and sampled frames below to judge the moment.'}</p>
                      </div>
                      <div className="analysis-evidence-block">
                        <span className="ingest-label">Revealed line</span>
                        <p>{analysisFocusResult.text}</p>
                      </div>
                    </div>

                    <div className="analysis-evidence-media">
                      <div className="analysis-evidence-block">
                        <span className="ingest-label">Representative thumbnail</span>
                        {analysisFocusThumbnailUrl ? (
                          <img src={analysisFocusThumbnailUrl} alt="Representative thumbnail for the focused interpretive moment" className="analysis-evidence-thumbnail" />
                        ) : (
                          <div className="analysis-evidence-thumbnail-placeholder">Representative thumbnail pending</div>
                        )}
                      </div>
                      <div className="analysis-evidence-block">
                        <span className="ingest-label">Representative frames</span>
                        <div className="analysis-sample-frame-strip">
                          {analysisFocusSampleFrames.length ? (
                            analysisFocusSampleFrames.map((frame) => (
                              <figure key={frame.sample_index} className="analysis-sample-frame-card">
                                <img src={frame.resolved_asset_url} alt={`Sample frame ${frame.sample_index + 1}`} className="analysis-sample-frame-image" />
                                <figcaption>{formatClock(frame.timestamp_ms)}</figcaption>
                              </figure>
                            ))
                          ) : (
                            <p className="muted">No sampled frames are stored for this moment yet.</p>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="analysis-evidence-block">
                    <span className="ingest-label">Surrounding context</span>
                    {analysisFocusContextSegments.length ? (
                      <div className="analysis-context-lines">
                        {analysisFocusContextSegments.map((segment) => (
                          <div key={segment.id} className={`analysis-context-line ${analysisFocusResult.segment_id === segment.id ? 'active' : ''}`.trim()}>
                            <span>{formatClock(segment.start_ms)}</span>
                            <strong>{segment.text}</strong>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="muted">Open the player for this result to review the surrounding transcript context here.</p>
                    )}
                  </div>
                </div>
              ) : (
                <EmptyState title="No evidence focus yet" text="Open a search result in the player to review the scene description, sampled frames, match basis, and surrounding context here." />
              )}
            </div>

            <div className="stack">
              <h3>Transcript Editing</h3>
              <p className="muted">Edit transcript text, save to reindex, and use undo within the current session.</p>
              <textarea
                className="mono"
                rows={12}
                value={viewerDraftText}
                onChange={(e) => {
                  setViewerDraftText(e.target.value)
                  pushUndoState(e.target.value)
                }}
                placeholder="Transcript raw text"
              />
              <div className="topbar-actions">
                <button type="button" className="ghost" disabled={viewerHistory.index < 1 || viewerSaving} onClick={undoViewerDraft}>
                  Undo
                </button>
                <button type="button" disabled={!viewerVideoId || viewerSaving} onClick={saveViewerTranscript}>
                  {viewerSaving ? 'Saving...' : 'Save + Reindex'}
                </button>
              </div>
            </div>
          </Card>
        </section>
      ) : null}

      {mode === 'tuning' ? (
        <section className="grid one">
          <Card title="Tuning" subtitle="Continuous Analysis feedback is summarized here and only becomes reportable once the threshold-gated review window is strong enough.">
            {tuningSummary ? (
              <div className="stack tuning-stack">
                <div className="grid two tuning-grid">
                  <div className="result-box tuning-status-box">
                    <div className="tuning-section-heading">
                      <div>
                        <strong>Live feedback window</strong>
                        <p className="muted">
                          Feedback since {tuningLiveWindow?.feedback_window_start_at ? formatDateTime(tuningLiveWindow.feedback_window_start_at) : 'the beginning of collection'}.
                        </p>
                      </div>
                      <span className={`search-fit-chip semantic ${tuningLiveWindow?.ready_for_report ? 'strong' : 'loose'}`}>
                        {tuningLiveWindow?.ready_for_report ? 'Ready for report' : 'Collecting more signal'}
                      </span>
                    </div>
                    <div className="tuning-metric-grid">
                      <div className="tuning-metric">
                        <span>New votes</span>
                        <strong>{tuningLiveWindow?.new_vote_count || 0} / {tuningLiveWindow?.minimum_new_votes_required || 50}</strong>
                      </div>
                      <div className="tuning-metric">
                        <span>Recurring canonical queries</span>
                        <strong>{tuningLiveWindow?.recurring_canonical_query_count || 0} / {tuningLiveWindow?.minimum_recurring_canonical_queries_required || 10}</strong>
                      </div>
                      <div className="tuning-metric">
                        <span>Positive rate</span>
                        <strong>{formatPercent((tuningLiveWindow?.positive_vote_count || 0) / Math.max(1, tuningLiveWindow?.new_vote_count || 0))}</strong>
                      </div>
                      <div className="tuning-metric">
                        <span>Latest vote</span>
                        <strong>{formatDateTime(tuningLiveWindow?.feedback_window_end_at)}</strong>
                      </div>
                    </div>
                    <div className="tuning-progress-group">
                      <div className="tuning-progress-row">
                        <span>Vote coverage</span>
                        <div className="tuning-progress-track" aria-hidden="true">
                          <div
                            className="tuning-progress-fill"
                            style={{ width: `${Math.min(100, ((tuningLiveWindow?.new_vote_count || 0) / Math.max(1, tuningLiveWindow?.minimum_new_votes_required || 50)) * 100)}%` }}
                          />
                        </div>
                      </div>
                      <div className="tuning-progress-row">
                        <span>Recurring query coverage</span>
                        <div className="tuning-progress-track" aria-hidden="true">
                          <div
                            className="tuning-progress-fill"
                            style={{ width: `${Math.min(100, ((tuningLiveWindow?.recurring_canonical_query_count || 0) / Math.max(1, tuningLiveWindow?.minimum_recurring_canonical_queries_required || 10)) * 100)}%` }}
                          />
                        </div>
                      </div>
                    </div>
                    <div className="topbar-actions">
                      <button type="button" className="ghost" disabled={tuningLoading || tuningBusy} onClick={() => loadTuningSummary(token)}>
                        {tuningLoading ? 'Refreshing...' : 'Refresh summary'}
                      </button>
                      <button
                        type="button"
                        disabled={tuningBusy || tuningLoading || !tuningLiveWindow?.ready_for_report}
                        onClick={handleCreateTuningReport}
                      >
                        {tuningBusy ? 'Working...' : 'Create report'}
                      </button>
                    </div>
                  </div>

                  <div className="result-box tuning-status-box">
                    <div className="tuning-section-heading">
                      <div>
                        <strong>Latest report</strong>
                        <p className="muted">
                          Snapshot the current tuning window, then explicitly approve it before any live comparison.
                        </p>
                      </div>
                      <span className={`search-fit-chip semantic ${tuningLatestReport?.status === 'APPROVED_FOR_LIVE_COMPARISON' ? 'strong' : 'medium'}`}>
                        {tuningLatestReport ? tuningLatestReport.status.replaceAll('_', ' ') : 'No report yet'}
                      </span>
                    </div>
                    {tuningLatestReport ? (
                      <div className="stack">
                        <div className="tuning-metric-grid">
                          <div className="tuning-metric">
                            <span>Votes captured</span>
                            <strong>{tuningLatestReport.new_vote_count}</strong>
                          </div>
                          <div className="tuning-metric">
                            <span>Recurring canonical queries</span>
                            <strong>{tuningLatestReport.recurring_canonical_query_count}</strong>
                          </div>
                          <div className="tuning-metric">
                            <span>Created</span>
                            <strong>{formatDateTime(tuningLatestReport.created_at)}</strong>
                          </div>
                          <div className="tuning-metric">
                            <span>Approved</span>
                            <strong>{formatDateTime(tuningLatestReport.approved_at)}</strong>
                          </div>
                        </div>
                        <p className="muted">
                          Window: {tuningLatestReport.feedback_window_start_at ? formatDateTime(tuningLatestReport.feedback_window_start_at) : 'collection start'} to {formatDateTime(tuningLatestReport.feedback_window_end_at)}.
                        </p>
                        <div className="topbar-actions">
                          <button
                            type="button"
                            disabled={tuningBusy || tuningLatestReport.status === 'APPROVED_FOR_LIVE_COMPARISON'}
                            onClick={() => handleApproveTuningReport(tuningLatestReport.id)}
                          >
                            {tuningLatestReport.status === 'APPROVED_FOR_LIVE_COMPARISON' ? 'Approved for live comparison' : 'Approve live comparison'}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <EmptyState
                        title="No report snapshot yet"
                        text="Keep collecting feedback in Analysis. Once the vote and recurring-query thresholds are met, create a report here for review."
                      />
                    )}
                  </div>
                </div>

                <div className="grid two tuning-grid">
                  <Card title="Canonical Query Coverage" subtitle="Recurring queries carry the most useful tuning signal because they survive one-off browsing noise.">
                    {tuningLiveWindow?.canonical_queries?.length ? (
                      <ul className="list tuning-query-list">
                        {tuningLiveWindow.canonical_queries.map((item) => (
                          <li key={item.canonical_query} className="tuning-query-row">
                            <div className="list-main">
                              <strong>{item.canonical_query}</strong>
                              <span className="muted">
                                {item.variants.join(', ')} · {item.distinct_query_run_count} query runs · avg rank {item.average_rank_position ? item.average_rank_position.toFixed(1) : 'n/a'}
                              </span>
                            </div>
                            <div className="search-result-meta">
                              <span className={`search-fit-chip semantic ${item.is_recurring ? 'strong' : 'loose'}`}>
                                {item.is_recurring ? 'Recurring' : 'Single run'}
                              </span>
                              <span className="search-fit-chip lexical">{item.vote_count} votes</span>
                              <span className="search-fit-chip semantic medium">{formatPercent(item.approval_rate)} positive</span>
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <EmptyState title="No canonical query clusters yet" text="Leave thumbs feedback in Analysis to start building recurring query coverage." />
                    )}
                  </Card>

                  <Card title="Score And Retrieval Signals" subtitle="Use these two slices to spot whether weak behavior is clustering by fit band or by retrieval mode.">
                    <div className="stack">
                      <div className="tuning-band-grid">
                        {(tuningLiveWindow?.score_bands || []).map((band) => (
                          <div key={band.label} className="tuning-band-card">
                            <strong>{band.label}</strong>
                            <span>{band.vote_count} votes</span>
                            <span className="muted">{formatPercent(band.approval_rate)} positive</span>
                          </div>
                        ))}
                      </div>
                      <ul className="list">
                        {(tuningLiveWindow?.retrieval_modes || []).map((item) => (
                          <li key={item.label}>
                            <div className="list-main">
                              <strong>{item.label}</strong>
                              <span className="muted">
                                {item.vote_count} votes · {formatPercent(item.approval_rate)} positive
                              </span>
                            </div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </Card>
                </div>

                <Card title="Recent Feedback Activity" subtitle="This confirms the live Analysis thumbs are being collected and tied back to canonical query groups.">
                  {tuningLiveWindow?.recent_feedback?.length ? (
                    <ul className="list">
                      {tuningLiveWindow.recent_feedback.map((item, index) => (
                        <li key={`${item.created_at}-${item.query_text}-${index}`}>
                          <div className="list-main">
                            <strong>{item.query_text}</strong>
                            <span className="muted">
                              {item.canonical_query} · rank {item.rank_position} · {item.semantic_score != null ? formatSemanticFit(item.semantic_score) : 'keyword only'} · {item.retrieval_mode}
                            </span>
                          </div>
                          <div className="search-result-meta">
                            <span className={`search-fit-chip semantic ${item.is_relevant ? 'strong' : 'loose'}`}>
                              {item.is_relevant ? 'Helpful' : 'Weak'}
                            </span>
                            {item.lexical_match ? <span className="search-fit-chip lexical">Keyword match</span> : null}
                            <span className="search-fit-chip semantic medium">{formatDateTime(item.created_at)}</span>
                          </div>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState title="No feedback captured yet" text="Use the thumbs in Analysis search rows and they will show up here." />
                  )}
                </Card>
              </div>
            ) : (
              <EmptyState title="Loading tuning data" text="Open this tab after leaving a few thumbs votes in Analysis to review the live summary and report readiness." />
            )}
          </Card>
        </section>
      ) : null}

      {mode === 'publicPreview' ? (
        <section className={`grid one ${isPreviewRouteLocked ? 'preview-route-shell' : ''}`.trim()}>
          {isPreviewRouteLocked ? publicPreviewShell : (
            <Card
              title={publicSurfaceTitle}
              subtitle={IS_PUBLIC_APP ? '' : 'Curated read-only surface built only from the records you have explicitly published.'}
            >
              {publicPreviewShell}
            </Card>
          )}
        </section>
      ) : null}

      {mode === 'ops' ? (
        <>
        <section className="grid two ops-grid ops-dashboard">
        <Card className="ops-card" bodyClassName="ops-card-body" title="Projects" subtitle="Create and edit project containers.">
          <div className="ops-card-section">
            <div className="ops-section-heading">
              <h3>Create project</h3>
              <p>Add a project for objects, videos, transcripts, and clips.</p>
            </div>
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault()
                withSubmit(
                  () => createProject(token, projectForm),
                  'Project created.'
                )
              }}
            >
              <input
                placeholder="Project name"
                value={projectForm.name}
                onChange={(e) => setProjectForm((prev) => ({ ...prev, name: e.target.value }))}
                required
              />
              <textarea
                placeholder="Description"
                value={projectForm.description}
                onChange={(e) => setProjectForm((prev) => ({ ...prev, description: e.target.value }))}
              />
              <button disabled={busy}>Create project</button>
            </form>
          </div>

          <div className="ops-card-section ops-manage-panel">
            <div className="ops-section-heading">
              <h3>Manage projects</h3>
              <p>Load a project into the editor without stretching the column.</p>
            </div>
            <div className="ops-scroll-zone">
              {sortedProjects.length ? (
                <ul className="list">
                  {sortedProjects.map((project) => (
                    <li key={project.id}>
                      <div className="list-main">
                        <strong>{project.name}</strong>
                        <span>{shortId(project.id)}</span>
                      </div>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() =>
                          setProjectEdit({
                            id: project.id,
                            name: project.name,
                            description: project.description || ''
                          })
                        }
                      >
                        Edit
                      </button>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState title="No projects yet" text="Create one project to unlock object, video, and transcript work." />}
            </div>

            {projectEdit.id ? (
              <div className="result-box">
                <p>
                  Editing project: <strong>{shortId(projectEdit.id)}</strong>
                </p>
                <input
                  value={projectEdit.name}
                  onChange={(e) => setProjectEdit((prev) => ({ ...prev, name: e.target.value }))}
                  placeholder="Project title"
                />
                <textarea
                  value={projectEdit.description}
                  onChange={(e) => setProjectEdit((prev) => ({ ...prev, description: e.target.value }))}
                  placeholder="Project description"
                />
                <div className="topbar-actions">
                  <button type="button" disabled={busy} onClick={saveProjectEdits}>
                    Save project
                  </button>
                  <button type="button" className="ghost" onClick={() => setProjectEdit({ id: '', name: '', description: '' })}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </Card>

        <Card className="ops-card" bodyClassName="ops-card-body" title="Objects" subtitle="Attach objects to projects and manage model state.">
          <div className="ops-card-section">
            <div className="ops-section-heading">
              <h3>Create object</h3>
              <p>Add an object, then manage its details and model from the selector below.</p>
            </div>
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault()
                withSubmit(() => {
                  let metadata = null
                  if (objectForm.metadata_json.trim()) {
                    try {
                      metadata = JSON.parse(objectForm.metadata_json)
                    } catch {
                      throw new Error('Metadata JSON is invalid. Enter valid JSON or leave it blank.')
                    }
                  }
                  return createObject(token, {
                    project_id: objectForm.project_id,
                    name: objectForm.name,
                    description: objectForm.description,
                    metadata_json: metadata
                  })
                }, 'Object created.')
              }}
            >
              <select
                value={objectForm.project_id}
                onChange={(e) => setObjectForm((prev) => ({ ...prev, project_id: e.target.value }))}
                required
              >
                <option value="">Select project</option>
                {sortedProjects.map((project) => (
                  <option value={project.id} key={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <input
                placeholder="Object name"
                value={objectForm.name}
                onChange={(e) => setObjectForm((prev) => ({ ...prev, name: e.target.value }))}
                required
              />
              <textarea
                placeholder="Description"
                value={objectForm.description}
                onChange={(e) => setObjectForm((prev) => ({ ...prev, description: e.target.value }))}
              />
              <textarea
                placeholder='Metadata JSON (optional) e.g. {"tag":"archive"}'
                value={objectForm.metadata_json}
                onChange={(e) => setObjectForm((prev) => ({ ...prev, metadata_json: e.target.value }))}
              />
              <button disabled={busy}>Create object</button>
            </form>
          </div>

          <div className="ops-card-section ops-manage-panel">
            <div className="ops-section-heading">
              <h3>Manage objects</h3>
              <p>Use the project filter and dropdown selector instead of a long object list.</p>
            </div>

            {sortedObjects.length ? (
              <>
                <div className="ops-inline-grid">
                  <div className="field">
                    <label className="ingest-label" htmlFor="object-project-filter">Project filter</label>
                    <select
                      id="object-project-filter"
                      value={objectManagerProjectId}
                      onChange={(e) => {
                        setObjectManagerProjectId(e.target.value)
                        setObjectEdit({ id: '', name: '', description: '', metadata_json: '' })
                      }}
                    >
                      <option value="">All projects</option>
                      {sortedProjects.map((project) => (
                        <option value={project.id} key={project.id}>
                          {project.name}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="field">
                    <label className="ingest-label" htmlFor="object-select">Object selector</label>
                    <select
                      id="object-select"
                      value={objectEdit.id}
                      onChange={(e) => beginObjectEdit(filteredObjectsForManager.find((objectRow) => objectRow.id === e.target.value) || null)}
                    >
                      <option value="">Select object</option>
                      {filteredObjectsForManager.map((objectRow) => (
                        <option value={objectRow.id} key={objectRow.id}>
                          {objectRow.name}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {!filteredObjectsForManager.length ? <EmptyState title="No objects in this filter" text="Choose another project or create a new object for this project." /> : null}

                {selectedObjectDetails ? (
                  <div className="ops-scroll-zone">
                    <div className="result-box ops-editor-box">
                      <p>
                        Editing object: <strong>{shortId(selectedObjectDetails.id)}</strong>
                      </p>
                      <p>Project: {sortedProjects.find((project) => project.id === selectedObjectDetails.project_id)?.name || 'Unknown project'}</p>
                      <input
                        value={objectEdit.name}
                        onChange={(e) => setObjectEdit((prev) => ({ ...prev, name: e.target.value }))}
                        placeholder="Object title"
                      />
                      <textarea
                        value={objectEdit.description}
                        onChange={(e) => setObjectEdit((prev) => ({ ...prev, description: e.target.value }))}
                        placeholder="Object description"
                      />
                      <textarea
                        value={objectEdit.metadata_json}
                        onChange={(e) => setObjectEdit((prev) => ({ ...prev, metadata_json: e.target.value }))}
                        placeholder="Metadata JSON (optional)"
                      />
                      <div className="result-box model-status-box">
                        <p>
                          3D model: <strong>{opsObjectModel ? opsObjectModel.original_filename : 'No model uploaded'}</strong>
                        </p>
                        <p>
                          Revision: {opsObjectModel ? opsObjectModel.revision_number : 'n/a'}
                        </p>
                        <p>
                          {opsObjectModel
                            ? 'Replace the active .glb to keep annotations and mark them for review.'
                            : 'One active .glb model is supported for each object.'}
                        </p>
                        <p className="muted">Accepted file type: `.glb`.</p>
                        <input
                          ref={opsModelFileInputRef}
                          type="file"
                          accept=".glb,model/gltf-binary"
                          onChange={(e) => setOpsObjectModelFile(e.target.files?.[0] || null)}
                        />
                        <div className="topbar-actions">
                          <button
                            type="button"
                            disabled={modelBusy || !opsObjectModelFile}
                            onClick={() => uploadModelForObject(selectedObjectDetails.id, opsObjectModelFile)}
                          >
                            {opsObjectModel ? 'Replace .glb model' : 'Upload .glb model'}
                          </button>
                          {opsObjectModel ? (
                            <button
                              type="button"
                              className="ghost"
                              disabled={modelBusy}
                              onClick={() => removeModelForObject(selectedObjectDetails.id)}
                            >
                              Remove model
                            </button>
                          ) : null}
                        </div>
                      </div>
                      <div className="topbar-actions">
                        <button type="button" disabled={busy} onClick={saveObjectEdits}>
                          Save object
                        </button>
                        <button type="button" className="ghost" onClick={() => setObjectEdit({ id: '', name: '', description: '', metadata_json: '' })}>
                          Clear selection
                        </button>
                      </div>
                    </div>
                  </div>
                ) : <EmptyState title="Select an object" text="Choose an object to edit metadata, link a model, or manage revisions." />}
              </>
            ) : (
              <EmptyState title="No objects yet" text="Create an object after setting up a project." />
            )}
          </div>
        </Card>
      </section>

        <section className="grid two ops-grid ops-dashboard">
          <Card className="ops-card" bodyClassName="ops-card-body" title="Videos" subtitle="Upload or register media, then queue processing.">
          <div className="ops-card-section">
            <div className="ops-section-heading">
              <h3>Add video</h3>
              <p>Upload from this machine or choose a file already in inventory.</p>
            </div>
            <div className="stack ops-upload-intro">
              <p className="upload-zone-title">Upload from this machine</p>
              <p className="muted">Accepted: MP4, MOV, MKV, WebM, WAV, MP3, M4A, FLAC, AAC.</p>
              <input
                type="file"
                accept="video/*,audio/*,.mp4,.mov,.mkv,.webm,.wav,.mp3,.m4a,.flac,.aac"
                onChange={(e) => setMediaUpload(e.target.files?.[0] || null)}
              />
              <div className="result-box ops-selected-file">
                <p>Local file</p>
                <p><strong>{mediaUpload ? mediaUpload.name : 'No local file selected'}</strong></p>
              </div>
            </div>
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault()
                  if (mediaUpload) {
                    handleMediaUpload()
                    return
                  }
                withSubmit(
                  async () => {
                    if (!videoForm.media_path) {
                      throw new Error('Select a media file from inventory before creating a video.')
                    }
                    const result = await createVideo(token, {
                      project_id: videoForm.project_id,
                      object_id: videoForm.object_id || null,
                      stable_video_id: videoForm.stable_video_id,
                      title: videoForm.title || selectedMedia?.filename || 'Untitled video',
                      source_path: videoForm.media_path,
                        original_filename: selectedMedia?.filename || undefined,
                        enqueue_transcode: true,
                        auto_transcribe_if_missing: true
                    })
                    setVideoForm((prev) => ({
                      ...prev,
                      stable_video_id: nextStableVideoId(),
                      title: ''
                    }))
                    return result
                  },
                  'Video registered and jobs queued.'
                )
              }}
            >
              <select
                value={videoForm.project_id}
                onChange={(e) => setVideoForm((prev) => ({ ...prev, project_id: e.target.value }))}
                required
              >
                <option value="">Select project</option>
                {sortedProjects.map((project) => (
                  <option value={project.id} key={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <select
                value={videoForm.object_id}
                onChange={(e) => setVideoForm((prev) => ({ ...prev, object_id: e.target.value }))}
              >
                <option value="">Select object (optional)</option>
                {sortedObjects
                  .filter((obj) => !videoForm.project_id || obj.project_id === videoForm.project_id)
                  .map((obj) => (
                    <option value={obj.id} key={obj.id}>
                      {obj.name}
                    </option>
                  ))}
              </select>
              <input
                placeholder="Stable video id"
                value={videoForm.stable_video_id}
                onChange={(e) => setVideoForm((prev) => ({ ...prev, stable_video_id: e.target.value }))}
                required
              />
              <input
                placeholder="Title (optional, defaults to filename)"
                value={videoForm.title}
                onChange={(e) => setVideoForm((prev) => ({ ...prev, title: e.target.value }))}
              />
              <select
                value={videoForm.media_path}
                onChange={(e) => setVideoForm((prev) => ({ ...prev, media_path: e.target.value }))}
                  required={!mediaUpload}
              >
                <option value="">Choose inventory file</option>
                {mediaFiles.map((file) => (
                  <option value={file.path} key={file.path}>
                    {file.filename}
                  </option>
                ))}
              </select>
                <div className="result-box ops-selected-file">
                  <p>Inventory file</p>
                  <p><strong>{selectedMedia ? selectedMedia.filename : 'No inventory file selected'}</strong></p>
                  <p className="muted">Upload and register in one step with a local file, or register an existing inventory file here.</p>
                </div>
                <button disabled={busy}>{mediaUpload ? 'Upload, register, and queue' : 'Register and queue'}</button>
            </form>
          </div>
          <div className="ops-card-section ops-manage-panel">
            <div className="ops-section-heading">
              <h3>Recent videos</h3>
              <p>Review recent jobs and load a record into the editor.</p>
            </div>

            <div className="ops-scroll-zone">
              {sortedVideos.length ? (
                <ul className="list">
                  {sortedVideos.slice(0, 8).map((video) => (
                    <li key={video.id}>
                      <div className="list-main video-list-main">
                        <strong>{video.title}</strong>
                        {video.status === 'TRANSCODING' ? (
                          (() => {
                            const progress = transcodeProgressForVideo(video, statusNowMs)
                            if (!progress) return null
                            return (
                              <div className="video-transcode-inline" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent}>
                                <div className="video-transcode-inline-fill" style={{ width: `${progress.percent}%` }} />
                                <span>
                                  {transcodeStageLabel(progress.stage)} {progress.percent}%
                                  {shouldShowEta(progress.remainingMs) ? ` · ETA ${formatEta(progress.remainingMs)}` : ''}
                                </span>
                              </div>
                            )
                          })()
                        ) : (
                          <span>{video.status} · {shortId(video.id)}</span>
                        )}
                      </div>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => setVideoEdit({ id: video.id, title: video.title, object_id: video.object_id || '' })}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={busy}
                        onClick={() => removeVideo(video.id)}
                      >
                        Delete
                      </button>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState title="No videos yet" text="Add a video from a local upload or inventory file to start processing." />}
            </div>

            {videoEdit.id ? (
              <div className="result-box">
                <p>
                  Editing video: <strong>{shortId(videoEdit.id)}</strong>
                </p>
                <input
                  value={videoEdit.title}
                  onChange={(e) => setVideoEdit((prev) => ({ ...prev, title: e.target.value }))}
                  placeholder="Video title"
                />
                <select
                  value={videoEdit.object_id}
                  onChange={(e) => setVideoEdit((prev) => ({ ...prev, object_id: e.target.value }))}
                >
                  <option value="">No object linked</option>
                  {sortedObjects.map((obj) => (
                    <option value={obj.id} key={obj.id}>
                      {obj.name}
                    </option>
                  ))}
                </select>
                <div className="topbar-actions">
                  <button type="button" disabled={busy} onClick={saveVideoEdits}>
                    Save video
                  </button>
                  <button type="button" className="ghost" disabled={busy} onClick={() => removeVideo(videoEdit.id)}>
                    Delete video
                  </button>
                  <button type="button" className="ghost" onClick={() => setVideoEdit({ id: '', title: '', object_id: '' })}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </Card>

          <Card className="ops-card" bodyClassName="ops-card-body" title="Transcripts" subtitle="Load transcript text and queue indexing.">
            <div className="ops-card-section ops-ingest-section">
              <div className="ops-section-heading">
                <h3>Add transcript</h3>
                <p>Select a video, load a file or paste text, then queue indexing.</p>
              </div>
              <div className="stack ingest-panel">
              <div className="ingest-header">
                <p className="muted">
                  Choose a video, confirm format and language, then ingest.
                </p>
              </div>

              <div className="ingest-import">
                <p className="ingest-label">Import Transcript File</p>
                <p className="muted">Accepted: `.srt`, `.vtt`, `.txt`.</p>
                <input
                  type="file"
                  accept=".txt,.srt,.vtt,text/plain"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) {
                      handleTranscriptFileImport(file).catch((err) => setError(err.message))
                    }
                  }}
                />
              </div>

          <form
              className="stack ingest-form"
            onSubmit={(e) => {
              e.preventDefault()
              withSubmit(async () => {
                const result = await ingestTranscript(token, transcriptForm)
                setLastResult(result)
                setTranscriptForm((prev) => ({ ...prev, title: result.transcript.title }))
              }, 'Transcript ingested and indexing queued.')
            }}
          >
              <div className="field">
                <label className="ingest-label" htmlFor="transcript-video-select">Video</label>
                <select
                  id="transcript-video-select"
                  value={transcriptForm.video_id}
                  onChange={(e) => {
                    const nextVideoId = e.target.value
                    const nextVideo = sortedVideos.find((video) => video.id === nextVideoId)
                    setTranscriptForm((prev) => ({
                      ...prev,
                      video_id: nextVideoId,
                      title: prev.title || (nextVideo ? `${nextVideo.title} Transcript` : '')
                    }))
                  }}
                  required
                >
                  <option value="">Select video</option>
                  {sortedVideos.map((video) => (
                    <option value={video.id} key={video.id}>
                      {video.title} ({shortId(video.id)})
                    </option>
                  ))}
                </select>
            </div>

              <div className="ingest-grid">
                <div className="field">
                  <label className="ingest-label" htmlFor="transcript-title-input">Transcript title</label>
                  <input
                    id="transcript-title-input"
                    value={transcriptForm.title}
                    onChange={(e) => setTranscriptForm((prev) => ({ ...prev, title: e.target.value }))}
                    placeholder="Transcript title"
                  />
                </div>

                <div className="field">
                  <label className="ingest-label" htmlFor="transcript-format-select">Format</label>
                  <select
                    id="transcript-format-select"
                    value={transcriptForm.format}
                    onChange={(e) => setTranscriptForm((prev) => ({ ...prev, format: e.target.value }))}
                  >
                    <option value="SRT">SRT</option>
                    <option value="VTT">VTT</option>
                    <option value="PLAIN">PLAIN</option>
                  </select>
                </div>

                <div className="field">
                  <label className="ingest-label" htmlFor="transcript-language-input">Language</label>
                  <input
                    id="transcript-language-input"
                    value={transcriptForm.language}
                    onChange={(e) => setTranscriptForm((prev) => ({ ...prev, language: e.target.value }))}
                    placeholder="en"
                  />
                </div>
              </div>

              <div className="field">
                <label className="ingest-label" htmlFor="transcript-raw-input">Transcript text</label>
                <textarea
                  id="transcript-raw-input"
                  className="mono"
                  rows={7}
                  value={transcriptForm.raw_text}
                  onChange={(e) => setTranscriptForm((prev) => ({ ...prev, raw_text: e.target.value }))}
                  required
                />
              </div>
              <div className="topbar-actions ingest-actions">
                <button disabled={busy}>Ingest transcript</button>
              </div>
                </form>
              </div>
            </div>

            <div className="ops-card-section ops-manage-panel">
              <div className="ops-section-heading">
                <h3>Manage transcripts</h3>
                <p>Recent ingest runs stay contained in their own scrollable panel.</p>
              </div>

              {lastResult ? (
                <div className="result-box">
                  <p>
                    Last ingest: <strong>{shortId(lastResult.transcript.id)}</strong>
                  </p>
                  <p>Title: {lastResult.transcript.title}</p>
                  <p>Segments: {lastResult.segments_indexed}</p>
                  <p>Index job: {lastResult.indexing_job_id || 'queue unavailable'}</p>
                </div>
              ) : <EmptyState title="No transcript runs yet" text="Load transcript text for a video to create the first indexed transcript." />}

              <div className="ops-scroll-zone">
                {transcripts.length ? (
                  <ul className="list">
                    {transcripts.slice(0, 8).map((transcript) => (
                      <li key={transcript.id}>
                        <div className="list-main">
                          <strong>{transcript.title}</strong>
                          <span>{shortId(transcript.id)}</span>
                        </div>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => setTranscriptEdit({ id: transcript.id, title: transcript.title })}
                        >
                          Edit
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              {transcriptEdit.id ? (
                <div className="result-box">
                  <p>
                    Editing transcript: <strong>{shortId(transcriptEdit.id)}</strong>
                  </p>
                  <input
                    value={transcriptEdit.title}
                    onChange={(e) => setTranscriptEdit((prev) => ({ ...prev, title: e.target.value }))}
                    placeholder="Transcript title"
                  />
                  <div className="topbar-actions">
                    <button type="button" disabled={busy} onClick={saveTranscriptEdits}>
                      Save transcript
                    </button>
                    <button type="button" className="ghost" onClick={() => setTranscriptEdit({ id: '', title: '' })}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
        </Card>
      </section>
        </>
      ) : null}

      {mode === 'model3d' ? (
        <section className="grid one">
          <Card title="3D Model" subtitle="Upload models, place annotations, and review linked evidence.">
            <div className="model-workspace-shell">
              <div className="model-toolbar">
                <div className="field">
                  <label className="ingest-label" htmlFor="model-project-select">Project</label>
                  <select
                    id="model-project-select"
                    value={modelProjectId}
                    onChange={(e) => setModelProjectId(e.target.value)}
                  >
                    <option value="">Select project</option>
                    {sortedProjects.map((project) => (
                      <option value={project.id} key={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="field">
                  <label className="ingest-label" htmlFor="model-object-select">Object</label>
                  <select
                    id="model-object-select"
                    value={modelObjectId}
                    onChange={(e) => setModelObjectId(e.target.value)}
                  >
                    <option value="">Select object</option>
                    {filteredObjectsForModelTab.map((objectRow) => (
                      <option value={objectRow.id} key={objectRow.id}>
                        {objectRow.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="result-box model-toolbar-status">
                  <p>
                    Model status: <strong>{modelDetail ? `Revision ${modelDetail.revision_number}` : 'No model uploaded'}</strong>
                  </p>
                  <p>Annotations: {modelAnnotations.length}</p>
                  <p>Needs review: {modelReviewRequiredCount}</p>
                </div>
              </div>

              {!activeModelObject ? <EmptyState title="Select a project and object" text="Choose the object you want to review before uploading a model or placing annotations." /> : (
                <div className="model-layout">
                  <section className="model-canvas-panel">
                    <div className="result-box model-upload-box">
                      <p>
                        Object: <strong>{activeModelObject.name}</strong>
                      </p>
                      <p>
                        {modelDetail
                          ? `Current model: ${modelDetail.original_filename}`
                          : 'No .glb model uploaded yet for this object.'}
                      </p>
                      <p className="muted">Accepted file type: `.glb`. One active model is supported for each object.</p>
                      <input
                        ref={modelFileInputRef}
                        type="file"
                        accept=".glb,model/gltf-binary"
                        onChange={(e) => setModelUploadFile(e.target.files?.[0] || null)}
                      />
                      <div className="topbar-actions">
                        <button
                          type="button"
                          disabled={modelBusy || !modelUploadFile}
                          onClick={() => uploadModelForObject(modelObjectId, modelUploadFile)}
                        >
                          {modelDetail ? 'Replace .glb model' : 'Upload .glb model'}
                        </button>
                        {modelDetail ? (
                          <button type="button" className="ghost" disabled={modelBusy} onClick={() => removeModelForObject(modelObjectId)}>
                            Remove model
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="ghost"
                          disabled={modelBusy || !modelDetail}
                          onClick={() => {
                            setModelViewMode((current) => {
                              const next = !current
                              setModelTransformDraft(defaultModelTransform(savedModelTransform))
                              setModelCameraDraft(savedModelCameraView)
                              return next
                            })
                            setPlacementMode(false)
                          }}
                        >
                          {modelViewMode ? 'Cancel transform model' : 'Transform model'}
                        </button>
                        <button type="button" className="ghost" disabled={modelBusy || !modelDetail} onClick={startModelAnnotationPlacement}>
                          {placementMode ? 'Click model to place pin...' : 'Place annotation'}
                        </button>
                      </div>

                      {modelViewMode && modelDetail ? (
                        <div className="result-box model-transform-editor">
                          <div className="model-transform-copy">
                            <p className="muted">Use Sketchfab-style viewing controls while transforming: drag to orbit, right-drag or two-finger drag to pan, and scroll to zoom.</p>
                            <p className="muted">Hold Option and drag to rotate the model itself. Hold Shift and drag to reposition the model. Then save once.</p>
                          </div>
                          <div className="topbar-actions">
                            <button
                              type="button"
                              className="ghost"
                              onClick={() => {
                                setModelTransformDraft(defaultModelTransform(savedModelTransform))
                                setModelCameraDraft(savedModelCameraView)
                              }}
                            >
                              Reset transform
                            </button>
                            <button type="button" disabled={modelBusy} onClick={saveModelView}>
                              Save model view
                            </button>
                          </div>
                        </div>
                      ) : null}
                    </div>

                      <ModelCanvas
                        modelUrl={modelFileUrl}
                      modelTransform={currentModelTransform}
                      defaultCameraView={currentModelCameraView}
                      annotations={modelAnnotations}
                      selectedAnnotationId={selectedModelAnnotation?.id || annotationEdit.id}
                      placementMode={placementMode}
                      editMode={modelViewMode}
                      cameraViewKey={`${modelDetail?.id ?? 'none'}:${modelDetail?.updated_at ?? 'none'}:main`}
                      showAnnotationLabels={!annotationOverlayOpen}
                      isLoading={modelLoading}
                      loadingEyebrow={activeModelObject ? 'Object workspace' : '3D workspace'}
                      loadingTitle={modelDetail ? 'Updating 3D workspace...' : 'Preparing 3D workspace...'}
                      loadingMessage={
                        modelDetail
                          ? 'Keeping the current stage visible while the next model revision and annotations load.'
                          : 'Loading the object model, saved view, and annotation set for review.'
                      }
                        emptyTitle="No 3D model"
                        emptyMessage="Upload a .glb model for this object to begin spatial review and annotation placement."
                        backgroundColor={isPrivateAuthoringSurface ? '#f3f5f9' : undefined}
                        onSurfacePick={handleModelSurfacePick}
                      onTransformChange={handleModelTransformChange}
                      onCameraViewChange={handleModelCameraViewChange}
                      onSelectAnnotation={openModelAnnotationOverlay}
                    />

                    <div className="card model-side-card model-annotations-card-wide">
                      <div className="card-header">
                        <h2>Annotations</h2>
                        <p>Filter and review annotations attached to the active object model.</p>
                      </div>
                      <div className="card-body model-side-body">
                        <div className="topbar-actions model-filter-row">
                          <button type="button" className={annotationFilter === 'ALL' ? '' : 'ghost'} onClick={() => setAnnotationFilter('ALL')}>
                            All
                          </button>
                          <button type="button" className={annotationFilter === 'REVIEW_REQUIRED' ? '' : 'ghost'} onClick={() => setAnnotationFilter('REVIEW_REQUIRED')}>
                            Needs Review
                          </button>
                          <button type="button" className={annotationFilter === 'ACTIVE' ? '' : 'ghost'} onClick={() => setAnnotationFilter('ACTIVE')}>
                            Active
                          </button>
                        </div>
                        <div className="ops-scroll-zone model-annotation-list model-annotation-list-wide">
                          {filteredModelAnnotations.length ? (
                            <ul className="list">
                              {filteredModelAnnotations.map((annotation) => (
                                <li
                                  key={annotation.id}
                                  className={selectedModelAnnotation?.id === annotation.id ? 'model-annotation-item selected' : 'model-annotation-item'}
                                >
                                  <div className="list-main video-list-main">
                                    <strong>{annotation.title}</strong>
                                    <span>
                                      {annotation.is_published ? 'Published' : 'Private'} · {annotation.review_status} · {formatClock(annotation.start_ms)}-{formatClock(annotation.end_ms)}
                                    </span>
                                  </div>
                                  <div className="topbar-actions model-annotation-actions">
                                    <button
                                      type="button"
                                      className="ghost"
                                      disabled={publicationBusy}
                                      onClick={() => setAnnotationPublished(annotation.id, !annotation.is_published)}
                                    >
                                      {annotation.is_published ? 'Unpublish' : 'Publish'}
                                    </button>
                                    <button type="button" className="ghost" onClick={() => selectModelAnnotation(annotation, { loadIntoEditor: true })}>
                                      Edit
                                    </button>
                                    <button type="button" className="ghost" onClick={() => openModelAnnotationOverlay(annotation)}>
                                      Preview
                                    </button>
                                    {annotation.review_status === 'REVIEW_REQUIRED' ? (
                                      <button type="button" className="ghost" disabled={modelBusy} onClick={() => markAnnotationReviewed(annotation.id)}>
                                        Mark reviewed
                                      </button>
                                    ) : null}
                                    <button type="button" className="ghost" disabled={modelBusy} onClick={() => removeModelAnnotation(annotation.id)}>
                                      Delete
                                    </button>
                                  </div>
                                </li>
                              ))}
                            </ul>
                          ) : <EmptyState title="No annotations in this view" text="Change the filter or place a new annotation on the active model." />}
                        </div>
                      </div>
                    </div>
                  </section>

                  <section className="model-sidebar">
                    <div className="card model-side-card" ref={modelEditorRef}>
                      <div className="card-header">
                        <h2>Annotation Editor</h2>
                        <p>Link pins to video ranges and review them after model updates.</p>
                      </div>
                      <div className="card-body model-side-body">
                        <div className="stack">
                          {annotationMappingPrompt ? (
                            <div className="result-box annotation-mapping-inline">
                              <div className="annotation-mapping-copy">
                                <strong>Mapped range from Analysis</strong>
                                <p>
                                  {annotationMappingPromptVideo?.title || 'Linked video'} · {formatClock(annotationMappingPrompt.startMs)}-{formatClock(annotationMappingPrompt.endMs)}
                                </p>
                                <p className="muted">This range is already loaded into the draft below. Keep it as a new annotation, or append it to an existing annotation sequence.</p>
                              </div>
                              <div className="topbar-actions annotation-mapping-actions">
                                <button type="button" disabled={modelBusy || !modelDetail} onClick={startNewAnnotationFromMappingPrompt}>
                                  {modelDetail ? 'Place new pin for this range' : 'Keep as new annotation'}
                                </button>
                                <button
                                  type="button"
                                  className="ghost"
                                  disabled={modelBusy || !annotationMappingTargetId}
                                  onClick={addMappedRangeToExistingAnnotation}
                                >
                                  Add to existing annotation
                                </button>
                              </div>
                              {modelLoading ? (
                                <p className="muted">Loading existing annotations...</p>
                              ) : modelAnnotations.length ? (
                                <div className="field annotation-mapping-target">
                                  <label className="ingest-label" htmlFor="annotation-mapping-target">Add to existing annotation</label>
                                  <select
                                    id="annotation-mapping-target"
                                    value={annotationMappingTargetId}
                                    onChange={(e) => setAnnotationMappingTargetId(e.target.value)}
                                  >
                                    <option value="">Select annotation</option>
                                    {modelAnnotations.map((annotation) => (
                                      <option value={annotation.id} key={annotation.id}>
                                        {annotation.title} · {formatClock(annotation.start_ms)}-{formatClock(annotation.end_ms)}
                                      </option>
                                    ))}
                                  </select>
                                </div>
                              ) : (
                                <p className="muted">No existing annotations are available for this object yet.</p>
                              )}
                            </div>
                          ) : null}
                          <input
                            placeholder="Annotation title"
                            value={annotationEdit.title}
                            onChange={(e) => setAnnotationEdit((prev) => ({ ...prev, title: e.target.value }))}
                          />
                          <textarea
                            placeholder="Annotation note"
                            value={annotationEdit.description}
                            onChange={(e) => setAnnotationEdit((prev) => ({ ...prev, description: e.target.value }))}
                          />
                          <select
                            value={annotationEdit.video_id}
                            onChange={(e) => setAnnotationEdit((prev) => ({ ...prev, video_id: e.target.value }))}
                          >
                            <option value="">Linked video</option>
                            {filteredVideosForModelTab.map((video) => (
                              <option value={video.id} key={video.id}>
                                {video.title}
                              </option>
                            ))}
                          </select>
                          <div className="ops-inline-grid">
                            <input
                              placeholder="Start ms"
                              value={annotationEdit.start_ms}
                              onChange={(e) => setAnnotationEdit((prev) => ({ ...prev, start_ms: e.target.value }))}
                            />
                            <input
                              placeholder="End ms"
                              value={annotationEdit.end_ms}
                              onChange={(e) => setAnnotationEdit((prev) => ({ ...prev, end_ms: e.target.value }))}
                            />
                          </div>
                          <div className="result-box annotation-sequence-box">
                            <div className="annotation-sequence-header">
                              <strong>Annotation sequence</strong>
                              <button type="button" className="ghost" onClick={addCurrentClipToAnnotationSequence}>
                                Add current range
                              </button>
                            </div>
                            {annotationEdit.playlist.length ? (
                              <div className="annotation-sequence-list">
                                {annotationEdit.playlist.map((entry, index) => (
                                  <div className="annotation-sequence-item" key={`${entry.video_id}-${entry.start_ms}-${entry.end_ms}-${index}`}>
                                    <div className="annotation-sequence-copy">
                                      <strong>{entry.label || `Clip ${index + 1}`}</strong>
                                      <span>{formatClock(entry.start_ms)}-{formatClock(entry.end_ms)}</span>
                                    </div>
                                    <div className="topbar-actions annotation-sequence-actions">
                                      <button type="button" className="ghost" onClick={() => promoteAnnotationSequenceClip(index)}>
                                        Make first
                                      </button>
                                      <button type="button" className="ghost" onClick={() => moveAnnotationSequenceClip(index, -1)}>
                                        Up
                                      </button>
                                      <button type="button" className="ghost" onClick={() => moveAnnotationSequenceClip(index, 1)}>
                                        Down
                                      </button>
                                      <button type="button" className="ghost" onClick={() => removeAnnotationSequenceClip(index)}>
                                        Remove
                                      </button>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            ) : <p className="muted">Save the current range as-is, or add more ranges to build a sequence.</p>}
                          </div>
                          <div className="result-box">
                            <p>
                              Placement point: <strong>{annotationEdit.point_x && annotationEdit.point_y && annotationEdit.point_z ? 'Captured' : 'Not placed yet'}</strong>
                            </p>
                            <p>
                              {placementMode
                                ? 'Click the model surface to place the annotation pin.'
                                : 'Use Place annotation above, or capture a point below, then click the model.'}
                            </p>
                          </div>
                          <div className="topbar-actions">
                            <button type="button" disabled={modelBusy || !modelDetail} onClick={saveModelAnnotation}>
                              {annotationEdit.id ? 'Save annotation' : 'Create annotation'}
                            </button>
                            <button type="button" className="ghost" disabled={modelBusy || !modelDetail} onClick={startModelAnnotationPlacement}>
                              {placementMode ? 'Waiting for model click...' : 'Capture point on model'}
                            </button>
                            <button
                              type="button"
                              className="ghost"
                              onClick={() => {
                                setSelectedModelAnnotation(null)
                                setAnnotationEdit(emptyAnnotationDraft({ video_id: filteredVideosForModelTab[0]?.id || '' }))
                              }}
                            >
                              Clear form
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="card model-side-card">
                      <div className="card-header">
                        <h2>Selected Annotation</h2>
                        <p>Review the pin context here, then open the full evidence viewer when needed.</p>
                      </div>
                      <div className="card-body model-side-body">
                        {selectedModelAnnotation ? (
                          <div className="model-preview-card">
                            <div className="model-preview-copy">
                              <strong>{selectedModelAnnotation.title}</strong>
                              <span>{selectedModelAnnotationVideo?.title || 'Linked video unavailable'}</span>
                              <span>
                                {formatClock(selectedModelAnnotation.start_ms)}-{formatClock(selectedModelAnnotation.end_ms)}
                              </span>
                              <span>{selectedModelAnnotationPlaylist.length} mapped clip{selectedModelAnnotationPlaylist.length === 1 ? '' : 's'}</span>
                              <span>{selectedModelAnnotation.review_status}</span>
                              {selectedModelAnnotation.description ? <p>{selectedModelAnnotation.description}</p> : null}
                            </div>
                            {selectedModelAnnotationVideo ? (
                              <div className="model-preview-summary">
                                <div>
                                  <span className="muted">Video status</span>
                                  <strong>{selectedModelAnnotationVideo.status}</strong>
                                </div>
                                <div>
                                  <span className="muted">Object</span>
                                  <strong>{activeModelObject?.name || 'Unknown object'}</strong>
                                </div>
                              </div>
                            ) : (
                              <div className="model-preview-placeholder">Linked video preview unavailable.</div>
                            )}
                            <div className="topbar-actions">
                              <button type="button" onClick={() => openModelAnnotationOverlay(selectedModelAnnotation)}>
                                Open viewer
                              </button>
                              <button
                                type="button"
                                className="ghost"
                                onClick={() => {
                                  openSearchResult({
                                    video_id: selectedModelAnnotation.video_id,
                                    video_title: selectedModelAnnotationVideo?.title || selectedModelAnnotation.title,
                                    start_ms: selectedModelAnnotation.start_ms,
                                    end_ms: selectedModelAnnotation.end_ms
                                  })
                                }}
                              >
                                Open in Analysis
                              </button>
                              <button type="button" className="ghost" onClick={() => selectModelAnnotation(selectedModelAnnotation, { loadIntoEditor: true })}>
                                Edit annotation
                              </button>
                            </div>
                          </div>
                        ) : <EmptyState title="No annotation selected" text="Select a pin or annotation row to preview linked video context." />}
                      </div>
                    </div>

                  </section>

                  <div className="card model-side-card model-publication-card">
                    <div className="card-header">
                      <h2>Publication</h2>
                      <p>Promote the current object package from private authoring into the public-facing preview.</p>
                    </div>
                    <div className="card-body model-side-body">
                      <button
                        type="button"
                        className="publication-sync-button"
                        disabled={liveDatabaseSyncBusy || !activeModelObject}
                        onClick={() => setConfirmAction({
                          title: 'Sync Live Database?',
                          message: 'Promote the current published projection to the live database. This is a one-way operation. Published content will be visible on the configured public instance.',
                          confirmLabel: 'Sync Live Database',
                          onConfirm: () => { handleStartLiveDatabaseSync() }
                        })}
                      >
                        {liveDatabaseSyncBusy ? 'Syncing Live Database...' : 'Sync Live Database'}
                      </button>
                      <p className="muted">
                        Pushes every currently published package to the live public site using the one-way semantic_public promotion flow.
                      </p>

                      <div className="result-box publication-summary-box">
                        <p>
                          Object: <strong>{activeModelObject ? (activeModelObject.is_published ? 'Published' : 'Private') : 'No object selected'}</strong>
                        </p>
                        <p>
                          3D model: <strong>{modelDetail ? (modelDetail.is_published ? 'Published' : 'Private') : 'No model uploaded'}</strong>
                        </p>
                        <p>
                          Video: <strong>{currentModelPublicationVideo ? (currentModelPublicationVideo.is_published ? 'Published' : 'Private') : 'No video selected'}</strong>
                        </p>
                        <p>
                          Transcript: <strong>{currentModelPublicationTranscript ? (currentModelPublicationTranscript.is_published ? 'Published with video' : 'Private with video') : 'No transcript yet'}</strong>
                        </p>
                        <p>
                          Package: <strong>{packageStatusLabel}</strong>
                        </p>
                        <p>
                          Published annotations: <strong>{publishedModelAnnotationCount}</strong> / {modelAnnotations.length}
                        </p>
                        {!packageOpenNow && publicationBlockers.length > 0 ? (
                          <ul className="publication-blocker-list">
                            {publicationBlockers.map((blocker) => (
                              <li key={blocker.key}>{blocker.label}</li>
                            ))}
                          </ul>
                        ) : null}
                      </div>

                      <div className="field">
                        <label className="ingest-label" htmlFor="publication-video-select">Video</label>
                        <select
                          id="publication-video-select"
                          value={modelPublicationVideoId}
                          onChange={(event) => setModelPublicationVideoId(event.target.value)}
                        >
                          <option value="">Select linked video</option>
                          {publicationVideosForModelTab.map((video) => (
                            <option value={video.id} key={video.id}>
                              {video.title}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div className="publication-toggle-grid">
                        <button type="button" className={activeModelObject?.is_published ? '' : 'ghost'} disabled={publicationBusy || !activeModelObject} onClick={() => setObjectPublished(true)}>
                          Publish object
                        </button>
                        <button type="button" className="ghost" disabled={publicationBusy || !activeModelObject?.is_published} onClick={() => setObjectPublished(false)}>
                          Hide object
                        </button>
                        <button type="button" className={modelDetail?.is_published ? '' : 'ghost'} disabled={publicationBusy || !modelDetail} onClick={() => setModelPublished(true)}>
                          Publish 3D model
                        </button>
                        <button type="button" className="ghost" disabled={publicationBusy || !modelDetail?.is_published} onClick={() => setModelPublished(false)}>
                          Hide 3D model
                        </button>
                        <button type="button" className={currentModelPublicationVideo?.is_published ? '' : 'ghost'} disabled={publicationBusy || !currentModelPublicationVideo} onClick={() => setPublicationVideoPublished(true)}>
                          Publish video
                        </button>
                        <button type="button" className="ghost" disabled={publicationBusy || !currentModelPublicationVideo?.is_published} onClick={() => setPublicationVideoPublished(false)}>
                          Hide video
                        </button>
                        <button
                          type="button"
                          className={publicationPackagePublished ? '' : 'ghost'}
                          disabled={publicationBusy || !activeModelObject}
                          onClick={() => setConfirmAction({
                            title: 'Publish package?',
                            message: `Publish this object, its 3D model, video, transcript, and ${modelAnnotations.length} annotation${modelAnnotations.length === 1 ? '' : 's'} to the published projection. They will appear in Public Preview and can then be synced to the live database.`,
                            confirmLabel: 'Publish package',
                            onConfirm: () => { setCurrentObjectPackagePublished(true) }
                          })}
                        >
                          Publish package
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={publicationBusy || !publicationPackageHasPublishedContent}
                          onClick={() => setConfirmAction({
                            title: 'Hide package?',
                            message: `Hide this object, its 3D model, video, transcript, and ${modelAnnotations.length} annotation${modelAnnotations.length === 1 ? '' : 's'} from the published projection. They will no longer appear in Public Preview.`,
                            confirmLabel: 'Hide package',
                            onConfirm: () => { setCurrentObjectPackagePublished(false) }
                          })}
                        >
                          Hide package
                        </button>
                      </div>

                      {/*
                       * Slice E: Notify followers toggle.
                       *
                       * Per-package opt-in. Default OFF (founder direction
                       * 2026-04-27). When ON, the next publish + sync of
                       * this package fires a digest email via the public
                       * notify-me subscriber list (apps/api/app/services/
                       * notify_digest_worker.py — Slice E-4). When OFF,
                       * publish + sync proceed as before with no digest.
                       *
                       * The trigger is intentionally NOT auto-fired on
                       * is_published transition — the digest must wait
                       * until the public projection actually contains the
                       * synced content (otherwise subscribers click into
                       * a 404). Slice E-4's admin endpoint is the
                       * post-sync trigger.
                       */}
                      <label className="notify-followers-toggle">
                        <input
                          type="checkbox"
                          checked={Boolean(modelDetail?.notify_followers_on_publish)}
                          disabled={publicationBusy || !modelDetail}
                          onChange={(event) => setNotifyFollowersOnPublish(event.target.checked)}
                        />
                        <span className="notify-followers-toggle__copy">
                          <strong>Notify followers when this package publishes</strong>
                          <span className="muted">
                            When checked, the next publish + sync of this package fires a digest
                            email to every subscriber on the public landing page&apos;s notify-me
                            list. Off by default; toggle on per package.
                          </span>
                        </span>
                      </label>

                      <div className="result-box publication-summary-box publication-sync-status" role="status" aria-live="polite">
                        <div className="publication-sync-status-header">
                          <strong>{liveDatabaseSyncStatus.stage_label || 'Live Database sync status'}</strong>
                          <span>{liveDatabaseSyncStateLabel(liveDatabaseSyncStatus.state)} · {liveDatabaseSyncProgress}%</span>
                        </div>
                        <div className="publication-sync-status-bar" aria-hidden="true">
                          <span style={{ width: `${liveDatabaseSyncProgress}%` }}></span>
                        </div>
                        <p>{liveDatabaseSyncStatus.detail || 'Push every currently published package to the live public site.'}</p>
                        {liveDatabaseSyncCountsAvailable ? (
                          <div className="publication-sync-metrics">
                            <span>{liveDatabaseSyncStatus.object_count} objects</span>
                            <span>{liveDatabaseSyncStatus.video_count} videos</span>
                            <span>{liveDatabaseSyncStatus.annotation_count} annotations</span>
                            <span>{liveDatabaseSyncStatus.media_file_count} media files</span>
                          </div>
                        ) : null}
                        {liveDatabaseSyncStatus.finished_at ? (
                          <p className="muted">Last finished: {formatDateTime(liveDatabaseSyncStatus.finished_at)}</p>
                        ) : liveDatabaseSyncStatus.started_at ? (
                          <p className="muted">Started: {formatDateTime(liveDatabaseSyncStatus.started_at)}</p>
                        ) : null}
                        {liveDatabaseSyncStatus.error ? <p className="muted">Last error: {liveDatabaseSyncStatus.error}</p> : null}
                      </div>

                      <div className="topbar-actions publication-actions">
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => navigateToUrl('/public')}
                        >
                          Open Public Browse
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={liveDatabaseSyncBusy || !activeModelObject?.is_published}
                          onClick={() => openObjectInPublicPreview(activeModelObject.id, {
                            projectId: activeModelObject.project_id,
                            videoId: currentModelPublicationVideo?.id || ''
                          })}
                        >
                          Open Public Preview
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </Card>
        </section>
      ) : null}

      {annotationOverlayOpen && selectedModelAnnotation ? (
        <div className="annotation-overlay-backdrop" onClick={(event) => {
          if (event.target === event.currentTarget) {
            closeAnnotationOverlay()
          }
        }}>
          <div className="annotation-overlay-shell">
            <div className="annotation-overlay-header">
              <div className="annotation-overlay-heading">
                <h2>{selectedModelAnnotation.title}</h2>
                <p>
                  {annotationOverlayClipVideo?.title || selectedModelAnnotationVideo?.title || 'Linked video unavailable'} · {formatClock(annotationOverlayClip?.start_ms || 0)}-{formatClock(annotationOverlayClip?.end_ms || 0)}
                </p>
              </div>
              <button type="button" className="ghost" onClick={closeAnnotationOverlay}>
                Close
              </button>
            </div>

            <div className="annotation-overlay-layout">
              <section className="annotation-overlay-video-panel">
                {selectedModelAnnotationPlaylist.length > 1 ? (
                  <div className="annotation-overlay-playlist-bar">
                    {selectedModelAnnotationPlaylist.map((entry, index) => (
                      <button
                        type="button"
                        key={`${entry.video_id}-${entry.start_ms}-${entry.end_ms}-${index}`}
                        className={annotationOverlayClipIndex === index ? 'annotation-playlist-chip active' : 'annotation-playlist-chip'}
                        onClick={() => {
                          setAnnotationOverlayClipIndex(index)
                          setAnnotationOverlayEnded(false)
                        }}
                      >
                        {entry.label || `Clip ${index + 1}`}
                      </button>
                    ))}
                  </div>
                ) : null}
                {selectedModelAnnotationUrl ? (
                  <video
                    ref={annotationOverlayVideoRef}
                    className="annotation-overlay-video"
                    src={selectedModelAnnotationUrl}
                    controls
                    autoPlay
                    preload="metadata"
                    onLoadedMetadata={(event) => {
                      const media = event.currentTarget
                      media.currentTime = Math.max(0, Number(annotationOverlayClip?.start_ms || 0)) / 1000
                      media.play().catch(() => null)
                    }}
                    onTimeUpdate={(event) => {
                      const media = event.currentTarget
                      const currentMs = Math.floor((media.currentTime || 0) * 1000)
                      const endMs = Math.max(0, Number(annotationOverlayClip?.end_ms || 0))
                      if (currentMs >= endMs && endMs > 0) {
                        finishAnnotationOverlayClip()
                        return
                      }
                      setAnnotationOverlayCurrentMs(currentMs)
                    }}
                    onEnded={finishAnnotationOverlayClip}
                  />
                ) : (
                  <div className="annotation-overlay-placeholder">
                    {annotationOverlayClipVideo?.status !== 'READY'
                      ? 'Linked video is not READY yet.'
                      : 'Linked video preview unavailable.'}
                  </div>
                )}
                <div className="annotation-overlay-summary">
                  <span>
                    Clip {Math.min(annotationOverlayClipIndex + 1, selectedModelAnnotationPlaylist.length || 1)} of {Math.max(selectedModelAnnotationPlaylist.length, 1)}
                  </span>
                  <span>{selectedModelAnnotation.review_status}</span>
                  <span>{activeModelObject?.name || 'Unknown object'}</span>
                  {annotationOverlayEnded ? <p>Clip finished. Viewer reset to the clip start and is paused.</p> : null}
                  {selectedModelAnnotation.description ? (
                    <p className="annotation-overlay-description">{selectedModelAnnotation.description}</p>
                  ) : null}
                </div>
              </section>

              <section className="annotation-overlay-side-panel">
                <div className="topbar-actions annotation-overlay-toggle-row">
                  <button type="button" className={annotationOverlayPanel === 'transcript' ? '' : 'ghost'} onClick={() => setAnnotationOverlayPanel('transcript')}>
                    Transcript
                  </button>
                  <button type="button" className={annotationOverlayPanel === 'model' ? '' : 'ghost'} onClick={() => setAnnotationOverlayPanel('model')}>
                    3D Model
                  </button>
                </div>

                {annotationOverlayPanel === 'transcript' ? (
                  <div className="annotation-overlay-transcript-panel">
                    {annotationOverlayLoading ? <p className="muted">Loading transcript context...</p> : null}
                    {annotationOverlayError ? <p className="error-banner">{annotationOverlayError}</p> : null}
                    {!annotationOverlayLoading && !annotationOverlayError ? (
                      annotationOverlaySegments.length ? (
                        <div className="annotation-overlay-transcript-list" ref={annotationOverlayTranscriptListRef}>
                          {annotationOverlaySegments.map((segment, index) => {
                            const inRange = Number(segment.start_ms) >= Number(annotationOverlayClip?.start_ms || 0)
                              && Number(segment.start_ms) <= Number(annotationOverlayClip?.end_ms || 0)
                            const active = index === annotationOverlaySegmentIndex
                            return (
                              <button
                                type="button"
                                key={segment.id}
                                data-overlay-segment-index={index}
                                className={active ? 'annotation-transcript-row active' : inRange ? 'annotation-transcript-row in-range' : 'annotation-transcript-row'}
                                onClick={() => jumpAnnotationOverlayToMs(Number(segment.start_ms || 0))}
                              >
                                <span>{formatClock(segment.start_ms)}</span>
                                <strong>{segment.text}</strong>
                              </button>
                            )
                          })}
                        </div>
                      ) : <EmptyState title="No transcript for this clip" text="This annotation has no transcript segments available yet." />
                    ) : null}
                  </div>
                ) : (
                  <div className="annotation-overlay-model-panel">
                    <ModelCanvas
                      modelUrl={modelFileUrl}
                      modelTransform={currentModelTransform}
                      defaultCameraView={currentModelCameraView}
                      annotations={modelAnnotations}
                      selectedAnnotationId={selectedModelAnnotation.id}
                      placementMode={false}
                      cameraViewKey={`${modelDetail?.id ?? 'none'}:${modelDetail?.updated_at ?? 'none'}:overlay`}
                      showAnnotationLabels={true}
                      isLoading={modelLoading}
                      loadingEyebrow="Annotation evidence"
                      loadingTitle="Preparing linked 3D view..."
                      loadingMessage="Loading the object stage and linked annotation positions for this evidence moment."
                      emptyTitle="No 3D model"
                      emptyMessage="This object does not have a linked .glb model available for the evidence view yet."
                      backgroundColor={isPrivateAuthoringSurface ? '#f3f5f9' : undefined}
                      onSurfacePick={null}
                      onTransformChange={null}
                      onCameraViewChange={handleModelCameraViewChange}
                      onSelectAnnotation={openModelAnnotationOverlay}
                    />
                  </div>
                )}
              </section>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  )
}
