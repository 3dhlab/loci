import assert from 'node:assert/strict'
import test from 'node:test'
import { reconcileTranscriptDraft } from './transcriptDraft.js'

const source = { videoId: 'video-a', token: 'synthetic-a', transcriptId: 'transcript-a', rawText: 'Original transcript' }
const transcript = { id: source.transcriptId, raw_text: source.rawText }
const refresh = (overrides = {}) => reconcileTranscriptDraft({ source, draftText: source.rawText, videoId: source.videoId, token: source.token, transcript, ...overrides })

test('a clean same-video draft follows fresh server text', () => {
  const result = refresh({ transcript: { ...transcript, raw_text: 'Replacement transcript' } })
  assert.equal(result.action, 'replace')
  assert.equal(result.text, 'Replacement transcript')
  assert.equal(result.source.rawText, result.text)
})

test('unsaved edits survive a same-source navigation refresh', () => {
  const result = refresh({ draftText: 'Local unsaved edit' })
  assert.equal(result.action, 'preserve')
  assert.equal(result.text, 'Local unsaved edit')
  assert.deepEqual(result.source, source)
})

test('a server replacement cannot silently overwrite or save a dirty draft', () => {
  const result = refresh({ draftText: 'Local unsaved edit', transcript: { ...transcript, raw_text: 'Server replacement' } })
  assert.equal(result.action, 'conflict')
  assert.equal(result.text, 'Local unsaved edit')
  assert.deepEqual(result.source, source)
})

test('a new transcript identity conflicts with an unsaved draft', () => {
  assert.equal(refresh({ draftText: 'Local edit', transcript: { ...transcript, id: 'transcript-b' } }).action, 'conflict')
})

test('missing then recreated transcript refreshes a clean draft', () => {
  const missing = refresh({ transcript: null })
  assert.equal(missing.action, 'replace')
  assert.equal(missing.text, '')
  const recreated = refresh({ source: missing.source, draftText: '', transcript: { id: 'transcript-b', raw_text: 'Recreated transcript' } })
  assert.equal(recreated.action, 'replace')
  assert.equal(recreated.text, 'Recreated transcript')
})

test('a missing transcript retains unsaved text until it recovers', () => {
  const missing = refresh({ draftText: 'Local edit', transcript: null })
  assert.equal(missing.action, 'conflict')
  assert.equal(missing.text, 'Local edit')
  assert.equal(refresh({ source: missing.source, draftText: missing.text }).action, 'preserve')
})

test('switching video or authenticated session replaces the previous draft', () => {
  for (const identity of [{ videoId: 'video-b' }, { token: 'synthetic-b' }]) {
    const result = refresh({ ...identity, draftText: 'Previous private edit', transcript: { id: 'transcript-b', raw_text: 'Current transcript' } })
    assert.equal(result.action, 'replace')
    assert.equal(result.text, 'Current transcript')
  }
})

test('server text that already equals the local edit becomes the new baseline', () => {
  const result = refresh({ draftText: 'Already saved', transcript: { ...transcript, raw_text: 'Already saved' } })
  assert.equal(result.action, 'replace')
  assert.equal(result.source.rawText, 'Already saved')
})
