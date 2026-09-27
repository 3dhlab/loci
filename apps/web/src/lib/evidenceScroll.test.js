/* Unit coverage for the evidence direct-load scroll policy (P2, 2026-07-09).
 * Pure — runs on Node's built-in runner: `node --test`. */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  centeredRailScrollLeft,
  hasExplicitMomentDeepLink,
  shouldAutoScrollOnLoad,
} from './evidenceScroll.js'

test('plain object URL (no params) does not auto-scroll — lands at top', () => {
  assert.equal(hasExplicitMomentDeepLink({}), false)
  assert.equal(shouldAutoScrollOnLoad({}), false)
  assert.equal(shouldAutoScrollOnLoad(undefined), false)
  assert.equal(shouldAutoScrollOnLoad({ annotation: '', clip: '', video: '', t: null, tScroll: null }), false)
})

test('explicit moment deep links auto-scroll', () => {
  assert.equal(shouldAutoScrollOnLoad({ annotation: 'abc' }), true)
  assert.equal(shouldAutoScrollOnLoad({ clip: 'clip-1' }), true)
  assert.equal(shouldAutoScrollOnLoad({ video: 'video-123' }), true)
})

test('a finite ?t= timestamp is an explicit deep link (including t=0)', () => {
  assert.equal(hasExplicitMomentDeepLink({ t: 5000 }), true)
  assert.equal(hasExplicitMomentDeepLink({ t: 0 }), true) // "jump to start" is still explicit
  assert.equal(shouldAutoScrollOnLoad({ t: 0 }), true)
  assert.equal(hasExplicitMomentDeepLink({ t: '5000' }), true)
  assert.equal(shouldAutoScrollOnLoad({ t: '0' }), true)
})

test('a non-finite t is ignored (not a deep link)', () => {
  assert.equal(hasExplicitMomentDeepLink({ t: NaN }), false)
  assert.equal(hasExplicitMomentDeepLink({ t: undefined }), false)
  assert.equal(hasExplicitMomentDeepLink({ t: 'NaN' }), false)
  assert.equal(hasExplicitMomentDeepLink({ t: '-1' }), false)
})

test('an explicit t_scroll restore auto-scrolls even without a moment param', () => {
  assert.equal(hasExplicitMomentDeepLink({ tScroll: 1200 }), false) // t_scroll is not a "moment"
  assert.equal(shouldAutoScrollOnLoad({ tScroll: 1200 }), true)
  assert.equal(shouldAutoScrollOnLoad({ tScroll: 0 }), true)
  assert.equal(shouldAutoScrollOnLoad({ tScroll: NaN }), false)
})

test('rail centering targets the horizontal midpoint inside the rail', () => {
  assert.equal(centeredRailScrollLeft({
    itemOffsetLeft: 600,
    itemOffsetWidth: 200,
    trackClientWidth: 400,
    trackScrollWidth: 1400,
  }), 500)
})

test('rail centering clamps at the start and end of the rail', () => {
  assert.equal(centeredRailScrollLeft({
    itemOffsetLeft: 20,
    itemOffsetWidth: 120,
    trackClientWidth: 400,
    trackScrollWidth: 1400,
  }), 0)
  assert.equal(centeredRailScrollLeft({
    itemOffsetLeft: 1280,
    itemOffsetWidth: 180,
    trackClientWidth: 400,
    trackScrollWidth: 1400,
  }), 1000)
})

test('rail centering returns a safe origin for incomplete geometry', () => {
  assert.equal(centeredRailScrollLeft({}), 0)
  assert.equal(centeredRailScrollLeft({
    itemOffsetLeft: 100,
    itemOffsetWidth: 100,
    trackClientWidth: 500,
    trackScrollWidth: 300,
  }), 0)
})
