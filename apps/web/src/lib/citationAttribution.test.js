/* Unit coverage for public citation attribution resolution (Studio P4.6).
 * Runs on Node's built-in runner: `node --test`. Pure — no DOM/three/React.
 *
 * These assert the user-reported fix: during an active video moment the speaker
 * must resolve from `citation_attribution_timeline` by the CURRENT playback time.
 * Mocked payloads mirror the public allowlist (video_id, start_ms, end_ms,
 * timestamp_ms, speaker_label, session_date, session_date_text). */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  selectCitationAttributionForFocus,
  normalizeCitationAttribution,
  normalizeCitationTimelineEntry,
} from './citationAttribution.js'

// mm:ss -> ms helper for readable expectations.
const at = (m, s) => (m * 60 + s) * 1000

// --- Synthetic Vessel: 03:51 -> Alex Example (QA corpus baseline) ---
const SPOON_VIDEO = 'synthetic-video-a'
const spoonTimeline = [
  { video_id: SPOON_VIDEO, start_ms: at(1, 0), end_ms: at(2, 30), timestamp_ms: at(1, 0), speaker_label: 'Blair Example', session_date: '2025-02-10', session_date_text: 'February 10, 2025' },
  { video_id: SPOON_VIDEO, start_ms: at(3, 51), end_ms: at(4, 3), timestamp_ms: at(3, 51), speaker_label: 'Alex Example', session_date: '2025-02-14', session_date_text: 'February 14, 2025' },
  { video_id: SPOON_VIDEO, start_ms: at(5, 0), end_ms: at(6, 0), timestamp_ms: at(5, 0), speaker_label: 'Casey Example', session_date: '2025-02-18', session_date_text: 'February 18, 2025' },
]

// --- Synthetic Large Model: 02:57 -> Morgan Example ---
const MASK_VIDEO = 'synthetic-video-b'
const maskTimeline = [
  { video_id: MASK_VIDEO, start_ms: at(0, 0), end_ms: at(2, 40), timestamp_ms: at(0, 0), speaker_label: 'Jordan Example', session_date: '2025-01-20', session_date_text: 'January 20, 2025' },
  { video_id: MASK_VIDEO, start_ms: at(2, 45), end_ms: at(3, 30), timestamp_ms: at(2, 57), speaker_label: 'Morgan Example', session_date: '2025-01-22', session_date_text: 'January 22, 2025' },
]

test('Synthetic Vessel 03:51 resolves Alex Example from the timeline', () => {
  const focus = { video_id: SPOON_VIDEO, seek_ms: at(3, 51), start_ms: at(3, 51), end_ms: at(4, 3) }
  const entry = selectCitationAttributionForFocus(spoonTimeline, focus, null)
  assert.equal(entry?.speaker_label, 'Alex Example')
  const attribution = normalizeCitationAttribution(entry)
  assert.equal(attribution.speakerLabel, 'Alex Example')
  assert.equal(attribution.recordedDateLabel, 'February 14, 2025')
  assert.equal(attribution.apaDateLabel, '2025, February 14')
  assert.equal(attribution.detailSpeakerLabel, 'Alex Example')
})

test('Synthetic Large Model 02:57 resolves Morgan Example from the timeline', () => {
  const focus = { video_id: MASK_VIDEO, seek_ms: at(2, 57) }
  const entry = selectCitationAttributionForFocus(maskTimeline, focus, null)
  assert.equal(entry?.speaker_label, 'Morgan Example')
  assert.equal(normalizeCitationAttribution(entry).speakerLabel, 'Morgan Example')
})

test('Synthetic Large Model 04:05 transition resolves Taylor Example at the next title-card handoff', () => {
  const transitionTimeline = [
    {
      video_id: MASK_VIDEO,
      start_ms: 237500,
      end_ms: 247500,
      timestamp_ms: 237500,
      speaker_label: 'Morgan Example',
      session_date: '2025-02-08',
      session_date_text: 'February 8, 2025',
    },
    {
      video_id: MASK_VIDEO,
      start_ms: 247500,
      end_ms: 257500,
      timestamp_ms: 247500,
      speaker_label: 'Taylor Example',
      session_date: '2025-02-07',
      session_date_text: 'February 7, 2025',
    },
  ]
  const entry = selectCitationAttributionForFocus(transitionTimeline, {
    video_id: MASK_VIDEO,
    seek_ms: 245090,
    start_ms: 245090,
    end_ms: 249060,
  }, null)
  assert.equal(entry?.speaker_label, 'Taylor Example')
  const attribution = normalizeCitationAttribution(entry)
  assert.equal(attribution.speakerLabel, 'Taylor Example')
  assert.equal(attribution.recordedDateLabel, 'February 7, 2025')
})

test('speaker follows the CURRENT playback time (not a static clip start)', () => {
  // The dialog is opened on the spoon; as the clock advances across speaker
  // windows the resolved speaker must change with it (the P4.6 blocker #1 fix).
  const early = selectCitationAttributionForFocus(spoonTimeline, { video_id: SPOON_VIDEO, seek_ms: at(1, 30) }, null)
  const mid = selectCitationAttributionForFocus(spoonTimeline, { video_id: SPOON_VIDEO, seek_ms: at(3, 55) }, null)
  const late = selectCitationAttributionForFocus(spoonTimeline, { video_id: SPOON_VIDEO, seek_ms: at(5, 30) }, null)
  assert.equal(early?.speaker_label, 'Blair Example')
  assert.equal(mid?.speaker_label, 'Alex Example')
  assert.equal(late?.speaker_label, 'Casey Example')
})

test('seek_ms is preferred over start_ms for the live-time match', () => {
  // A selected clip keeps a stable start_ms (clip window) but seek_ms follows the
  // clock; the speaker must resolve by seek_ms.
  const focus = { video_id: SPOON_VIDEO, start_ms: at(3, 51), end_ms: at(4, 3), seek_ms: at(5, 30) }
  const entry = selectCitationAttributionForFocus(spoonTimeline, focus, null)
  assert.equal(entry?.speaker_label, 'Casey Example')
})

test('a focus in a gap picks the nearest entry by timestamp', () => {
  // 02:42 falls between the two mask windows (2:40 end, 2:45 start) -> nearest.
  const entry = selectCitationAttributionForFocus(maskTimeline, { video_id: MASK_VIDEO, seek_ms: at(2, 42) }, null)
  assert.equal(entry?.speaker_label, 'Morgan Example') // 2:57 timestamp is closer than 0:00
})

test('overlapping windows resolve to the entry whose timestamp is nearest the focus', () => {
  const overlap = [
    { video_id: SPOON_VIDEO, start_ms: at(3, 0), end_ms: at(4, 30), timestamp_ms: at(3, 5), speaker_label: 'Wide Window' },
    { video_id: SPOON_VIDEO, start_ms: at(3, 45), end_ms: at(4, 0), timestamp_ms: at(3, 52), speaker_label: 'Tight Window' },
  ]
  const entry = selectCitationAttributionForFocus(overlap, { video_id: SPOON_VIDEO, seek_ms: at(3, 51) }, null)
  assert.equal(entry?.speaker_label, 'Tight Window')
})

test('a different video id falls back (no cross-video attribution)', () => {
  const fallback = { speaker_label: 'Fallback Person', attribution_mode: 'attributed' }
  const entry = selectCitationAttributionForFocus(spoonTimeline, { video_id: 'video-other', seek_ms: at(3, 51) }, fallback)
  assert.equal(entry, fallback)
  // and with no fallback -> null -> Unavailable
  const none = selectCitationAttributionForFocus(spoonTimeline, { video_id: 'video-other', seek_ms: at(3, 51) }, null)
  assert.equal(none, null)
  assert.equal(normalizeCitationAttribution(none).speakerLabel, '')
  assert.equal(normalizeCitationAttribution(none).detailSpeakerLabel, 'Unavailable')
})

test('a focus without a seek/start time or video id falls back', () => {
  assert.equal(selectCitationAttributionForFocus(spoonTimeline, { video_id: SPOON_VIDEO }, null), null)
  assert.equal(selectCitationAttributionForFocus(spoonTimeline, { seek_ms: at(3, 51) }, null), null)
  assert.equal(selectCitationAttributionForFocus([], { video_id: SPOON_VIDEO, seek_ms: 0 }, 'fb'), 'fb')
})

test('withheld/anonymous attribution modes suppress the speaker label', () => {
  const withheld = normalizeCitationAttribution({ speaker_label: 'Hidden Name', attribution_mode: 'withheld' })
  assert.equal(withheld.speakerLabel, '')
  assert.equal(withheld.detailSpeakerLabel, 'Unavailable')
  const anon = normalizeCitationAttribution({ speaker_label: 'Hidden Name', attribution_mode: 'anonymous' })
  assert.equal(anon.speakerLabel, '')
})

test('normalizeCitationTimelineEntry floors times and falls back timestamp->start', () => {
  const entry = normalizeCitationTimelineEntry({ video_id: SPOON_VIDEO, start_ms: 12345.9, speaker_label: 'X' })
  assert.equal(entry.timestamp_ms, 12345) // falls back to start_ms when timestamp_ms absent
  assert.equal(entry.start_ms, 12345)
  assert.equal(entry.end_ms, null)
  assert.equal(normalizeCitationTimelineEntry({ speaker_label: 'no video/ts' }), null)
})

// ===========================================================================
// P4.6.1 — academic citation FORMAT coverage (source identity vs locator).
// ===========================================================================
import {
  buildEvidenceCitationLead,
  buildFormattedEvidenceCitation,
  buildPlainTextEvidenceCitation,
  isLocatorLikeTitle,
} from './citationAttribution.js'

const FORMATS = ['apa', 'chicago', 'harvard', 'turabian']
// Tokens that must NEVER appear in copyable citations (raw media / private).
const LEAK_TOKENS = [
  '.mp4', '_FINAL', '_gdFINAL', '_semantic4k', 'sha256', '/media/', '/var/lib/',
  'object-models', 'presenter_name', 'raw_text_observed', 'model_run', 'storage_path',
]
function assertNoLeak(text, label) {
  for (const token of LEAK_TOKENS) {
    assert.ok(!text.includes(token), `${label}: leaked ${token} in: ${text}`)
  }
}

// ---- locator detection ----
test('isLocatorLikeTitle: Transcript/time strings are locators, real titles are not', () => {
  assert.equal(isLocatorLikeTitle('Transcript 00:00'), true)
  assert.equal(isLocatorLikeTitle('Transcript 03:07 - 03:20'), true)
  assert.equal(isLocatorLikeTitle('03:07'), true)
  assert.equal(isLocatorLikeTitle('01:30 - 01:45'), true)
  assert.equal(isLocatorLikeTitle("A synthetic example statement"), false)
  assert.equal(isLocatorLikeTitle('Synthetic Large Model'), false)
  assert.equal(isLocatorLikeTitle(''), false)
})

// ---- lead: title starts from the object, locators demoted ----
test('buildEvidenceCitationLead: object title leads; transcript/time locators demoted', () => {
  // Direct ?t= / transcript focus -> title is the object, not "Transcript 00:00".
  assert.equal(buildEvidenceCitationLead('Synthetic Vessel', 'Transcript 00:00'), 'Synthetic Vessel')
  assert.equal(buildEvidenceCitationLead('Synthetic Sculpture (Study Model)', '01:30 - 01:45'), 'Synthetic Sculpture (Study Model)')
  // A real curated moment title rides in front of the object as a subtitle.
  assert.equal(
    buildEvidenceCitationLead('Synthetic Large Model', "A synthetic example statement"),
    "A synthetic example statement, Synthetic Large Model"
  )
  // Identical moment/object collapses to the object.
  assert.equal(buildEvidenceCitationLead('Synthetic Bowl', 'Synthetic Bowl'), 'Synthetic Bowl')
})

// ---- Scenario A: named speaker + annotation moment ----
test('named speaker + annotation moment: speaker is source identity, moment+object in title (all formats)', () => {
  const input = {
    objectTitle: 'Synthetic Large Model',
    momentTitle: "A synthetic example statement",
    collectionName: 'Synthetic Demonstration Collection',
    speakerLabel: 'Morgan Example',
    speakerAuthorName: 'Example, Morgan',
    recordedDate: 'February 8, 2025',
    apaRecordedDate: '2025, February 8',
    focusType: 'Published moment',
    timeLabel: '03:07',
    excerpt: "A synthetic example statement also.",
    canonicalUrl: 'https://loci.example.org/evidence/objects/synthetic-large-model?annotation=abc',
    accessDate: 'July 2, 2026',
  }
  const out = Object.fromEntries(FORMATS.map((f) => [f, buildFormattedEvidenceCitation(f, input)]))

  // Source identity (speaker) present; object + moment title both appear; no leaks.
  for (const f of FORMATS) {
    assert.ok(out[f].includes('Synthetic Large Model'), `${f} missing object title`)
    assert.ok(out[f].includes("A synthetic example statement"), `${f} missing moment title`)
    assertNoLeak(out[f], f)
  }
  assert.ok(out.apa.startsWith('Example, Morgan. (2025, February 8). '), out.apa)
  assert.ok(out.apa.includes('[Published moment, 03:07]'), out.apa)
  assert.ok(out.apa.includes('Loci.'), out.apa)
  assert.ok(out.chicago.startsWith('Morgan Example.'), out.chicago)
  assert.ok(out.harvard.startsWith("Example, Morgan (February 8, 2025) 'A synthetic example statement, Synthetic Large Model'"), out.harvard)
  assert.ok(out.turabian.startsWith('Morgan Example.'), out.turabian)
})

// ---- Scenario B: speakerless transcript time at 00:00 ----
test('speakerless transcript at 00:00: title starts from object, never "Transcript 00:00" (all formats)', () => {
  const input = {
    objectTitle: 'Synthetic Vessel',
    momentTitle: 'Transcript 00:00', // synthetic locator fed by the ?t= wiring
    collectionName: 'Synthetic Demonstration Collection',
    speakerLabel: '', // unavailable
    speakerAuthorName: '',
    recordedDate: '',
    apaRecordedDate: '',
    focusType: 'Timestamp focus',
    timeLabel: '00:00',
    excerpt: '',
    canonicalUrl: 'https://loci.example.org/evidence/objects/synthetic-vessel?video=v1&t=0',
    accessDate: 'July 2, 2026',
  }
  const out = Object.fromEntries(FORMATS.map((f) => [f, buildFormattedEvidenceCitation(f, input)]))

  for (const f of FORMATS) {
    assert.ok(!out[f].includes('Transcript 00:00'), `${f} leaked the locator into the title: ${out[f]}`)
    assert.ok(out[f].includes('Synthetic Vessel'), `${f} missing object title`)
    assert.ok(!out[f].includes('Samuel') && !/Speaker:/.test(out[f]), `${f} invented/echoed a speaker`)
    assert.ok(out[f].includes('Loci'), `${f} missing platform`)
    assertNoLeak(out[f], f)
  }
  // APA is title-led (object), no author, n.d. for the missing date; time is a locator.
  assert.ok(out.apa.startsWith('Synthetic Vessel. (n.d.). '), out.apa)
  assert.ok(out.apa.includes('[Timestamp focus, 00:00]'), out.apa)
  // Harvard uses the platform as source identity when the speaker is unavailable.
  assert.ok(out.harvard.startsWith("Loci (no date) 'Synthetic Vessel'"), out.harvard)
  // Chicago/Turabian: no leading speaker sentence -> starts with the quoted object title.
  assert.ok(out.chicago.startsWith('"Synthetic Vessel.'), out.chicago)
  assert.ok(out.turabian.startsWith('"Synthetic Vessel.'), out.turabian)
})

// ---- Scenario C: Synthetic speaker speakerless boundary gap (date known, speaker not) ----
test('Synthetic speaker speakerless boundary gap: object/platform identity, date kept, no speaker (all formats)', () => {
  const input = {
    objectTitle: 'Synthetic Sculpture (Study Model)',
    momentTitle: '01:30 - 01:45', // bare window locator at a gap between named speakers
    collectionName: 'Synthetic Demonstration Collection',
    speakerLabel: '', // gap -> unavailable
    speakerAuthorName: '',
    recordedDate: 'February 23, 2025',
    apaRecordedDate: '2025, February 23',
    focusType: 'Timestamp focus',
    timeLabel: '01:30 - 01:45',
    excerpt: '',
    canonicalUrl: 'https://loci.example.org/evidence/objects/synthetic-sculpture?video=v9&t=90000',
    accessDate: 'July 2, 2026',
  }
  const out = Object.fromEntries(FORMATS.map((f) => [f, buildFormattedEvidenceCitation(f, input)]))
  for (const f of FORMATS) {
    assert.ok(out[f].includes('Synthetic Sculpture (Study Model)'), `${f} missing object title`)
    assert.ok(!out[f].includes('01:30 - 01:45') || out[f].includes('Time: 01:30 - 01:45') || out[f].includes(', 01:30 - 01:45]'),
      `${f} window should appear only as a locator, not the title`)
    assert.ok(!/^["A-Za-z].*(InventedSpeakerA|InventedSpeakerB)/.test(out[f]), `${f} invented a speaker`)
    assert.ok(out[f].includes('Loci'), `${f} missing platform`)
    assertNoLeak(out[f], f)
  }
  // Object leads; recorded date preserved; speaker absent.
  assert.ok(out.apa.startsWith('Synthetic Sculpture (Study Model). (2025, February 23). '), out.apa)
  assert.ok(out.harvard.startsWith("Loci (February 23, 2025) 'Synthetic Sculpture (Study Model)'"), out.harvard)
  assert.ok(out.chicago.includes('Recorded February 23, 2025.'), out.chicago)
})

// ---- No-leak guarantee across plain-text + all formats ----
test('no raw filename / storage path / private field leaks in any citation output', () => {
  const input = {
    objectTitle: 'Synthetic Container',
    momentTitle: 'Transcript 02:10',
    collectionName: 'Synthetic Demonstration Collection',
    speakerLabel: 'Avery Example',
    speakerAuthorName: 'Example, Avery',
    recordedDate: 'March 3, 2025',
    apaRecordedDate: '2025, March 3',
    focusType: 'Timestamp focus',
    timeLabel: '02:10',
    excerpt: 'A short spoken excerpt.',
    canonicalUrl: 'https://loci.example.org/evidence/objects/synthetic-container?video=v3&t=130000',
    accessDate: 'July 2, 2026',
  }
  assertNoLeak(buildPlainTextEvidenceCitation(input), 'plain')
  for (const f of FORMATS) {
    assertNoLeak(buildFormattedEvidenceCitation(f, input), f)
  }
  // Plain text also demotes the transcript locator from the title lead.
  assert.ok(buildPlainTextEvidenceCitation(input).startsWith('Synthetic Container.'),
    buildPlainTextEvidenceCitation(input))
})
