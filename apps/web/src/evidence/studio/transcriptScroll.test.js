import assert from 'node:assert/strict'
import test from 'node:test'

import { calculateTranscriptScrollTop } from './transcriptScroll.js'

const listMetrics = {
  scrollTop: 500,
  listHeight: 460,
  rowHeight: 44,
  scrollHeight: 4000,
}

test('transcript centering uses list-relative geometry in Console and Focus', () => {
  const consoleTop = calculateTranscriptScrollTop({ ...listMetrics, listTop: 900, rowTop: 1200 })
  const focusTop = calculateTranscriptScrollTop({ ...listMetrics, listTop: 100, rowTop: 400 })

  // Both rows sit 300px below their own list. Their page positions differ,
  // while their intended list scroll positions stay identical.
  assert.equal(consoleTop, 592)
  assert.equal(focusTop, consoleTop)
})

test('transcript centering clamps to the list scroll range and stays finite', () => {
  assert.equal(calculateTranscriptScrollTop({
    listTop: 100,
    rowTop: 100,
    scrollTop: 0,
    listHeight: 460,
    rowHeight: 44,
    scrollHeight: 800,
  }), 0)

  assert.equal(calculateTranscriptScrollTop({
    listTop: 100,
    rowTop: 700,
    scrollTop: 340,
    listHeight: 460,
    rowHeight: 44,
    scrollHeight: 800,
  }), 340)

  const invalidMeasurements = calculateTranscriptScrollTop({
    listTop: Number.NaN,
    rowTop: Number.POSITIVE_INFINITY,
    scrollTop: Number.NaN,
    listHeight: 460,
    rowHeight: 44,
    scrollHeight: 800,
  })
  assert.ok(Number.isFinite(invalidMeasurements) && invalidMeasurements >= 0 && invalidMeasurements <= 800 - 460)
})
