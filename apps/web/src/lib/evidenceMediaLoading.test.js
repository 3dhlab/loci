import test from 'node:test'
import assert from 'node:assert/strict'

import {
  evidenceTimelineDurationMs,
  evidenceVideoAccessibleName,
  evidenceVideoDisplayTitle,
  evidenceVideoPreload,
} from './evidenceMediaLoading.js'

test('plain evidence pages defer the full session video', () => {
  assert.equal(evidenceVideoPreload(), 'none')
  assert.equal(evidenceVideoPreload({ hasFocusedMoment: false, hasHandoff: false }), 'none')
})

test('explicit moment routes prepare video playback', () => {
  assert.equal(evidenceVideoPreload({ hasFocusedMoment: true }), 'auto')
})

test('an in-app playback handoff prepares video playback', () => {
  assert.equal(evidenceVideoPreload({ hasHandoff: true }), 'auto')
})

// ---- evidenceTimelineDurationMs ----
test('a deferred plain route still renders a timeline length from the published row', () => {
  assert.equal(
    evidenceTimelineDurationMs({ mediaDurationMs: 0, publishedDurationMs: 713984 }),
    713984
  )
})

test('media metadata overrides the published duration once it exists', () => {
  assert.equal(
    evidenceTimelineDurationMs({ mediaDurationMs: 713900, publishedDurationMs: 713984 }),
    713900
  )
})

test('an unknown length stays zero so the scrubber remains disabled', () => {
  assert.equal(evidenceTimelineDurationMs(), 0)
  assert.equal(evidenceTimelineDurationMs({}), 0)
  for (const bad of [null, undefined, 0, -1, Number.NaN, Number.POSITIVE_INFINITY, 'abc']) {
    assert.equal(
      evidenceTimelineDurationMs({ mediaDurationMs: bad, publishedDurationMs: bad }),
      0,
      `unexpected duration for ${String(bad)}`
    )
  }
})

test('a fractional media duration is floored to whole milliseconds', () => {
  assert.equal(evidenceTimelineDurationMs({ mediaDurationMs: 713983.7 }), 713983)
})

// ---- evidenceVideoAccessibleName ----
test('the video accessible name uses the object title, never the source filename', () => {
  const name = evidenceVideoAccessibleName({
    objectTitle: 'Synthetic Container',
    preload: 'none',
  })

  assert.ok(name.includes('Synthetic Container'))
  assert.ok(!/\.mp4/i.test(name))
  assert.ok(!/semantic4k/i.test(name))
  assert.ok(!/_/.test(name))
})

test('the activation hint appears only while the body is deferred', () => {
  const deferred = evidenceVideoAccessibleName({ objectTitle: 'Synthetic Bowl', preload: 'none' })
  const eager = evidenceVideoAccessibleName({ objectTitle: 'Synthetic Bowl', preload: 'auto' })

  assert.ok(/press play to load/i.test(deferred))
  assert.ok(!/press play/i.test(eager))
  assert.equal(eager, 'Session video: Synthetic Bowl')
})

test('a missing object title still yields a usable accessible name', () => {
  assert.equal(
    evidenceVideoAccessibleName({ objectTitle: '   ', preload: 'auto' }),
    'Session video'
  )
  assert.ok(evidenceVideoAccessibleName().startsWith('Session video'))
})

test('raw media filenames become a human-readable public video title', () => {
  assert.equal(evidenceVideoDisplayTitle({
    objectTitle: 'Synthetic Container',
    publishedTitle: 'synthetic-demo.mp4',
  }), 'Synthetic Container session video')
})

test('curator-written video titles remain intact', () => {
  assert.equal(evidenceVideoDisplayTitle({
    objectTitle: 'Synthetic Container',
    publishedTitle: 'Conversation about the medicine pot',
  }), 'Conversation about the medicine pot')
  assert.equal(evidenceVideoDisplayTitle({}), 'Session video')
})
