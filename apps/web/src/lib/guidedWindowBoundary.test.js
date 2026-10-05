import test from 'node:test'
import assert from 'node:assert/strict'
import { guidedWindowBoundary } from './guidedWindowBoundary.js'
const guided = (start, end) => ({ clips: [{ startMs: start, endMs: end }], index: 0, lastFrameMs: start, frameStepMs: 1000 / 30 })
test('exclusive windows retain the last decoded frame before the next chapter', () => {
  for (const [start, end] of [[0, 4000], [4000, 8000]]) {
    const state = guided(start, end)
    assert.equal(guidedWindowBoundary(state, start, 12000, true).finish, false)
    assert.equal(guidedWindowBoundary(state, end - 66.6667, 12000, true).finish, false)
    const result = guidedWindowBoundary(state, end - 33.3333, 12000, true)
    assert.equal(result.finish, true)
    assert.ok(result.stopMs >= start && result.stopMs < end)
    guidedWindowBoundary(state, end - 10, 12000)
    assert.equal(guidedWindowBoundary(state, end + 236, 12000).stopMs, result.stopMs)
  }
})
test('sequence reset uses the next window start and full final duration reaches 12 seconds', () => {
  const state = guided(8000, 12000)
  assert.equal(guidedWindowBoundary(state, 8000, 12000, true).finish, false)
  assert.equal(guidedWindowBoundary(state, 11966.6667, 12000, true).finish, false)
  assert.deepEqual(guidedWindowBoundary(state, 12000, 12000, true), { finish: true, stopMs: 12000 })
})
test('coarse fallback recovers an interior frame after boundary overshoot', () => {
  const state = guided(4000, 8000)
  guidedWindowBoundary(state, 7983, 12000)
  assert.deepEqual(guidedWindowBoundary(state, 8236, 12000), { finish: true, stopMs: 7983 })
})
