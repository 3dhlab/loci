/* Unit coverage for Studio P4 search grouping/filtering (node:test). */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  partitionStudioResults,
  filterStudioResults,
  availableStudioFilters,
  isSafeEvidenceUrl,
  resultSeekMs,
  STUDIO_SEARCH_GROUP_CAP,
} from './studioSearchGrouping.js'

const CURRENT = {
  videoStableId: 'synthetic-video-a',
  projectSlug: 'back-to-africa-heritage-archaeology',
  objectPublicId: 'synthetic-vessel',
}

const onPage = {
  segment_id: 's1', stable_video_id: 'synthetic-video-a', project_slug: 'back-to-africa-heritage-archaeology',
  object_public_id: 'synthetic-vessel', evidence_url: '/evidence/objects/synthetic-vessel?t=1000', start_ms: 1000,
}
const sameCollectionDiffObject = {
  segment_id: 's2', stable_video_id: 'synthetic-video-c', project_slug: 'back-to-africa-heritage-archaeology',
  object_public_id: 'synthetic-sculpture', evidence_url: '/evidence/objects/synthetic-sculpture?t=2000', start_ms: 2000,
}
const otherCollection = {
  segment_id: 's3', stable_video_id: 'video-999', project_slug: 'other-project',
  object_public_id: 'synthetic-tool', evidence_url: '/evidence/objects/synthetic-tool?t=3000', start_ms: 3000,
}
const RESULTS = [onPage, sameCollectionDiffObject, otherCollection]

test('partition splits on-page (same stable video) from the collection', () => {
  const { onPage: op, collection } = partitionStudioResults(RESULTS, CURRENT)
  assert.deepEqual(op.map((r) => r.segment_id), ['s1'])
  assert.deepEqual(collection.map((r) => r.segment_id), ['s2', 's3'])
})

test('partition caps each group and preserves rank order', () => {
  const many = Array.from({ length: 20 }, (_, i) => ({ segment_id: `x${i}`, stable_video_id: 'video-999' }))
  const { collection } = partitionStudioResults(many, CURRENT, STUDIO_SEARCH_GROUP_CAP)
  assert.equal(collection.length, STUDIO_SEARCH_GROUP_CAP)
  assert.equal(collection[0].segment_id, 'x0')
})

test('filter: session / collection / object / all', () => {
  assert.deepEqual(filterStudioResults(RESULTS, 'session', CURRENT).map((r) => r.segment_id), ['s1'])
  assert.deepEqual(filterStudioResults(RESULTS, 'collection', CURRENT).map((r) => r.segment_id), ['s1', 's2'])
  assert.deepEqual(filterStudioResults(RESULTS, 'object', CURRENT).map((r) => r.segment_id), ['s1'])
  assert.deepEqual(filterStudioResults(RESULTS, 'all', CURRENT).length, 3)
  assert.deepEqual(filterStudioResults(RESULTS, 'bogus', CURRENT).length, 3)
})

test('availableStudioFilters degrades when a field is absent from results', () => {
  const keys = availableStudioFilters(RESULTS, CURRENT).map((f) => f.key)
  assert.deepEqual(keys, ['all', 'session', 'collection', 'object'])
  // Results with no object_public_id and no project_slug -> only all + session.
  const bare = [{ segment_id: 'b', stable_video_id: 'synthetic-video-a' }]
  assert.deepEqual(availableStudioFilters(bare, CURRENT).map((f) => f.key), ['all', 'session'])
  // No current context at all -> only 'all'.
  assert.deepEqual(availableStudioFilters(RESULTS, {}).map((f) => f.key), ['all'])
})

test('isSafeEvidenceUrl accepts relative /evidence/objects and rejects escapes', () => {
  assert.equal(isSafeEvidenceUrl('/evidence/objects/synthetic-tool?t=3000'), true)
  assert.equal(isSafeEvidenceUrl('https://evil.example/evidence/objects/x'), false)
  assert.equal(isSafeEvidenceUrl('//evil.example/evidence/objects/x'), false)
  assert.equal(isSafeEvidenceUrl('/public'), false)
  assert.equal(isSafeEvidenceUrl('javascript:alert(1)'), false)
  assert.equal(isSafeEvidenceUrl(''), false)
  assert.equal(isSafeEvidenceUrl(null), false)
})

test('resultSeekMs prefers start_ms, falls back to context_start_ms, else 0', () => {
  assert.equal(resultSeekMs({ start_ms: 4200 }), 4200)
  assert.equal(resultSeekMs({ context_start_ms: 900 }), 900)
  assert.equal(resultSeekMs({}), 0)
})
