/* Public evidence citation attribution — pure, framework-free helpers.
 *
 * Extracted from EvidenceObjectApp (Studio P4.6) so the timeline speaker
 * resolution is unit-testable without React/three. These functions consume ONLY
 * the public, allowlisted citation fields (video_id, start_ms/end_ms/timestamp_ms,
 * speaker_label, session_date/…): no private provenance ever reaches them.
 *
 * `selectCitationAttributionForFocus` is the core: given the published
 * `citation_attribution_timeline`, a focus that carries { video_id, seek_ms|start_ms },
 * and a top-level fallback, it returns the timeline entry whose window contains the
 * focus time (nearest by timestamp on overlap), else the fallback. During active
 * playback the focus seek time follows the clock, so the resolved speaker tracks
 * whoever is speaking at the current moment.
 */

export function normalizeQueryValue(value) {
  if (typeof value !== 'string') {
    return ''
  }

  const trimmed = value.trim()
  return trimmed || ''
}

export function normalizeFiniteNumber(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function collapseWhitespace(value) {
  if (typeof value !== 'string') {
    return ''
  }

  return value.replace(/\s+/g, ' ').trim()
}

function parseIsoDateOnly(value) {
  const normalized = normalizeQueryValue(value)
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!match) {
    return null
  }

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])

  if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) {
    return null
  }

  if (month < 1 || month > 12 || day < 1 || day > 31) {
    return null
  }

  return { year, month, day }
}

export function formatExactCitationDate(value, { yearFirst = false } = {}) {
  const parts = parseIsoDateOnly(value)
  if (!parts) {
    return ''
  }

  const date = new Date(Date.UTC(parts.year, parts.month - 1, parts.day))
  const displayLabel = new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC'
  }).format(date)

  if (!yearFirst) {
    return displayLabel
  }

  const monthDayLabel = new Intl.DateTimeFormat('en-US', {
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC'
  }).format(date)

  return `${parts.year}, ${monthDayLabel}`
}

export function formatRecordedCitationDate(sessionDate, sessionDateText, sessionDatePrecision, format = 'default') {
  const normalizedText = collapseWhitespace(sessionDateText)
  const normalizedPrecision = normalizeQueryValue(sessionDatePrecision).toLowerCase()

  if (normalizedText && normalizedPrecision && normalizedPrecision !== 'day') {
    return normalizedText
  }

  const exactLabel = formatExactCitationDate(sessionDate, { yearFirst: format === 'apa' })
  return exactLabel || normalizedText
}

export function normalizeCitationAttribution(value) {
  const attribution = value && typeof value === 'object' ? value : null
  if (!attribution) {
    return {
      speakerLabel: '',
      speakerAuthorName: '',
      recordedDateLabel: '',
      apaDateLabel: '',
      detailSpeakerLabel: 'Unavailable',
      detailRecordedLabel: 'Unavailable',
    }
  }

  const attributionMode = normalizeQueryValue(attribution.attribution_mode || attribution.attributionMode).toLowerCase()
  const speakerLabel = collapseWhitespace(attribution.speaker_label || attribution.speakerLabel || '')
  const speakerSortName = collapseWhitespace(attribution.speaker_sort_name || attribution.speakerSortName || '')
  const sessionDate = normalizeQueryValue(attribution.session_date || attribution.sessionDate)
  const sessionDateText = collapseWhitespace(attribution.session_date_text || attribution.sessionDateText || '')
  const sessionDatePrecision = normalizeQueryValue(attribution.session_date_precision || attribution.sessionDatePrecision)
  const speakerAllowed = Boolean(speakerLabel)
    && attributionMode !== 'anonymous'
    && attributionMode !== 'withheld'
    && attributionMode !== 'unavailable'

  const recordedDateLabel = formatRecordedCitationDate(sessionDate, sessionDateText, sessionDatePrecision)
  const apaDateLabel = formatRecordedCitationDate(sessionDate, sessionDateText, sessionDatePrecision, 'apa')

  return {
    speakerLabel: speakerAllowed ? speakerLabel : '',
    speakerAuthorName: speakerAllowed ? (speakerSortName || speakerLabel) : '',
    recordedDateLabel,
    apaDateLabel,
    detailSpeakerLabel: speakerAllowed ? speakerLabel : 'Unavailable',
    detailRecordedLabel: recordedDateLabel || 'Unavailable',
  }
}

export function normalizeCitationTimelineEntry(value) {
  const entry = value && typeof value === 'object' ? value : null
  if (!entry) {
    return null
  }

  const videoId = normalizeQueryValue(entry.video_id ?? entry.videoId)
  const timestampMs = normalizeFiniteNumber(entry.timestamp_ms ?? entry.timestampMs ?? entry.start_ms ?? entry.startMs)
  if (!videoId || timestampMs === null) {
    return null
  }

  const startMs = normalizeFiniteNumber(entry.start_ms ?? entry.startMs)
  const endMs = normalizeFiniteNumber(entry.end_ms ?? entry.endMs)

  return {
    ...entry,
    video_id: videoId,
    timestamp_ms: Math.max(0, Math.floor(timestampMs)),
    start_ms: Math.max(0, Math.floor(startMs ?? timestampMs)),
    end_ms: endMs === null ? null : Math.max(0, Math.floor(endMs))
  }
}

// ---------------------------------------------------------------------------
// Citation formatting (APA / Chicago / Harvard / Turabian) — pure, framework
// free, extracted from EvidenceObjectApp (P4.6.1) so the academic format is
// unit-testable.
//
// Design: SOURCE IDENTITY (who/what) is kept separate from the LOCATOR (where in
// the record). Source identity = speaker when named, the object title, a curated
// moment/annotation title when applicable, and the Loci platform. Locator = focus
// type, time window, transcript excerpt, and canonical URL. A synthetic
// "Transcript HH:MM" (or a bare time/range) is a LOCATOR, never the citation
// title — see `isLocatorLikeTitle` / `buildEvidenceCitationLead`.
//
// No-raw-filename guarantee: these builders take only public, human-facing
// fields; the source-video filename (e.g. `_FINAL`, `_semantic4k`, `.mp4`) and
// any storage path / private provenance are never inputs and never emitted.
// ---------------------------------------------------------------------------

export const CITATION_SITE_NAME = 'Loci'
const CITATION_TRANSITION_HANDOFF_MS = 2500

// A momentTitle that is a synthetic locator, not source identity: "Transcript
// 03:07", "Transcript 03:07 - 03:20", or a bare "03:07" / "03:07 - 03:20" window.
export function isLocatorLikeTitle(value) {
  const normalized = collapseWhitespace(value)
  if (!normalized) {
    return false
  }
  if (/^transcript\b/i.test(normalized)) {
    return true
  }
  const time = '\\d{1,2}:\\d{2}(?::\\d{2})?'
  return new RegExp(`^${time}(?:\\s*[-–—]\\s*${time})?$`).test(normalized)
}

export function citationSentence(value) {
  const normalized = collapseWhitespace(value)
  if (!normalized) {
    return ''
  }

  return /[.!?]"?$/.test(normalized) ? normalized : `${normalized}.`
}

export function quotedCitationSentence(value) {
  const normalized = collapseWhitespace(value)
  if (!normalized) {
    return ''
  }

  return `"${citationSentence(normalized)}"`
}

export function parentheticalCitationNote(value, prefix = '') {
  const normalized = collapseWhitespace(value)
  if (!normalized) {
    return ''
  }

  return prefix ? `${prefix}: ${normalized}` : normalized
}

// The source-identity title. The object title is the anchor (the work). A real,
// curated moment/annotation title rides in front of it as a subtitle; a synthetic
// transcript/time locator is demoted so the title always STARTS from the object
// title for `?t=` / transcript / default focus.
export function buildEvidenceCitationLead(objectTitle, momentTitle) {
  const normalizedObjectTitle = collapseWhitespace(objectTitle)
  const normalizedMomentTitle = collapseWhitespace(momentTitle)

  const momentIsIdentity = Boolean(normalizedMomentTitle)
    && normalizedMomentTitle !== normalizedObjectTitle
    && !isLocatorLikeTitle(normalizedMomentTitle)

  if (momentIsIdentity) {
    return normalizedObjectTitle
      ? `${normalizedMomentTitle}, ${normalizedObjectTitle}`
      : normalizedMomentTitle
  }

  if (normalizedObjectTitle) {
    return normalizedObjectTitle
  }

  // No object title and only a locator-like moment title -> no title identity.
  return isLocatorLikeTitle(normalizedMomentTitle) ? '' : normalizedMomentTitle
}

export function buildPlainTextEvidenceCitation({
  objectTitle,
  momentTitle,
  collectionName,
  speakerLabel,
  recordedDate,
  focusType,
  timeLabel,
  excerpt,
  canonicalUrl,
  accessDate
}) {
  const lead = buildEvidenceCitationLead(objectTitle, momentTitle)
  // The raw source-video filename is intentionally NOT included — it leaks
  // internal media provenance (e.g. `_gdFINAL`, `_semantic4k`, hash suffixes) into
  // user-facing copyable text. The canonical URL + collection are the public anchors.
  const parts = [
    lead,
    speakerLabel ? `Speaker: ${speakerLabel}` : '',
    recordedDate ? `Recorded: ${recordedDate}` : '',
    collectionName ? `Collection: ${collectionName}` : '',
    focusType ? `Focus: ${focusType}` : '',
    timeLabel ? `Time: ${timeLabel}` : '',
    excerpt ? `Excerpt: "${excerpt}"` : '',
    canonicalUrl ? `URL: ${canonicalUrl}` : '',
    accessDate ? `Accessed: ${accessDate}` : ''
  ].filter(Boolean)

  return parts.length ? `${parts.join('. ')}.` : ''
}

export function buildFormattedEvidenceCitation(format, {
  objectTitle,
  momentTitle,
  collectionName,
  speakerLabel,
  speakerAuthorName,
  recordedDate,
  apaRecordedDate,
  focusType,
  timeLabel,
  excerpt,
  canonicalUrl,
  accessDate
}) {
  const lead = buildEvidenceCitationLead(objectTitle, momentTitle)
  // Title always resolves to source identity (never a locator-like moment title).
  const title = lead || collapseWhitespace(objectTitle) || 'Published evidence moment'
  const focusDescriptor = focusType || 'Published evidence moment'
  const hasSpeakerAttribution = Boolean(speakerLabel)
  const citationAuthor = speakerAuthorName || CITATION_SITE_NAME
  const citationSource = speakerLabel || CITATION_SITE_NAME
  const citationDate = recordedDate || ''
  const apaCitationDate = apaRecordedDate || recordedDate || ''
  const collectionPart = parentheticalCitationNote(collectionName, 'Collection')
  // No `Video: <filename>` part — see buildPlainTextEvidenceCitation.
  const focusPart = parentheticalCitationNote(focusDescriptor, 'Focus')
  const timePart = parentheticalCitationNote(timeLabel, 'Time')
  const excerptPart = excerpt ? `Excerpt: "${excerpt}"` : ''
  const mediumDescriptor = `[${focusDescriptor}${timeLabel ? `, ${timeLabel}` : ''}]`
  const contextParts = [collectionPart, focusPart, timePart, excerptPart].filter(Boolean)
  const detailParts = [collectionPart, focusPart, timePart, excerptPart].filter(Boolean)

  if (format === 'chicago') {
    return [
      hasSpeakerAttribution ? citationSentence(citationSource) : '',
      quotedCitationSentence(title),
      citationDate ? citationSentence(`Recorded ${citationDate}`) : '',
      citationSentence(CITATION_SITE_NAME),
      citationSentence(collectionPart),
      citationSentence(focusPart),
      citationSentence(timePart),
      excerptPart ? citationSentence(excerptPart) : '',
      accessDate ? citationSentence(`Accessed ${accessDate}`) : '',
      canonicalUrl || ''
    ].filter(Boolean).join(' ')
  }

  if (format === 'harvard') {
    return [
      `${citationAuthor} (${citationDate || 'no date'}) '${title}'${contextParts.length ? `, ${contextParts.join(', ')}` : ''}.`,
      canonicalUrl ? `Available at: ${canonicalUrl}${accessDate ? ` (Accessed: ${accessDate})` : ''}.` : ''
    ].filter(Boolean).join(' ')
  }

  if (format === 'turabian') {
    return [
      hasSpeakerAttribution ? citationSentence(citationSource) : '',
      quotedCitationSentence(title),
      citationDate ? citationSentence(`Recorded ${citationDate}`) : '',
      citationSentence(CITATION_SITE_NAME),
      citationSentence(collectionPart),
      citationSentence(focusPart),
      citationSentence(timePart),
      excerptPart ? citationSentence(excerptPart) : '',
      accessDate ? citationSentence(`Accessed ${accessDate}`) : '',
      canonicalUrl || ''
    ].filter(Boolean).join(' ')
  }

  return [
    hasSpeakerAttribution
      ? `${citationAuthor}. (${apaCitationDate || 'n.d.'}). ${title}. ${mediumDescriptor}.`
      : `${title}. (${apaCitationDate || 'n.d.'}). ${mediumDescriptor}.`,
    CITATION_SITE_NAME ? `${CITATION_SITE_NAME}.` : '',
    detailParts.length ? `${detailParts.join('. ')}.` : '',
    canonicalUrl ? `Retrieved ${accessDate ? `${accessDate}, ` : ''}from ${canonicalUrl}.` : ''
  ].filter(Boolean).join(' ')
}

export function selectCitationAttributionForFocus(timeline, focus, fallbackAttribution) {
  if (!Array.isArray(timeline) || !focus) {
    return fallbackAttribution || null
  }

  const focusVideoId = normalizeQueryValue(focus.video_id ?? focus.videoId)
  const focusMs = normalizeFiniteNumber(focus.seek_ms ?? focus.seekMs)
    ?? normalizeFiniteNumber(focus.start_ms ?? focus.startMs)
  if (!focusVideoId || focusMs === null) {
    return fallbackAttribution || null
  }

  const candidates = timeline
    .map(normalizeCitationTimelineEntry)
    .filter((entry) => entry && entry.video_id === focusVideoId)

  if (!candidates.length) {
    return fallbackAttribution || null
  }

  const sortedCandidates = [...candidates].sort((a, b) => {
    if (a.start_ms !== b.start_ms) {
      return a.start_ms - b.start_ms
    }
    return a.timestamp_ms - b.timestamp_ms
  })
  const rangedCandidates = candidates.filter((entry) => (
    focusMs >= entry.start_ms
    && (entry.end_ms === null || focusMs < entry.end_ms)
  ))
  const searchCandidates = rangedCandidates.length ? rangedCandidates : candidates

  const bestMatch = searchCandidates.reduce((bestEntry, entry) => {
    if (!bestEntry) {
      return entry
    }

    const bestDistance = Math.abs(bestEntry.timestamp_ms - focusMs)
    const nextDistance = Math.abs(entry.timestamp_ms - focusMs)
    if (nextDistance < bestDistance) {
      return entry
    }

    return bestEntry
  }, null)

  if (bestMatch && rangedCandidates.length === 1) {
    const bestSpeaker = collapseWhitespace(bestMatch.speaker_label || bestMatch.speakerLabel || '')
    const nextEntry = sortedCandidates.find((entry) => {
      if (entry === bestMatch) {
        return false
      }

      const nextSpeaker = collapseWhitespace(entry.speaker_label || entry.speakerLabel || '')
      if (!nextSpeaker || nextSpeaker === bestSpeaker) {
        return false
      }

      return entry.start_ms >= focusMs
        && entry.start_ms - focusMs <= CITATION_TRANSITION_HANDOFF_MS
    })

    if (nextEntry) {
      const bestDistance = Math.abs(bestMatch.timestamp_ms - focusMs)
      const nextDistance = Math.abs(nextEntry.timestamp_ms - focusMs)
      if (nextDistance < bestDistance) {
        return nextEntry
      }
    }
  }

  return bestMatch || fallbackAttribution || null
}
