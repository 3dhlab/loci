/* Unit coverage for the adaptive 3D load policy (commit 50ad63b).
 *
 * Runs on Node's built-in test runner (no new dependency): `node --test`.
 * The pure detectors take an injectable `env`, so every policy branch is tested
 * deterministically; `useModelLoadPolicy` is exercised through a real React
 * render (react-dom/server) for its initial-state derivation. Live media-query
 * re-evaluation is covered by the Playwright smoke in
 * apps/web/scripts/modelLoadPolicySmoke.cjs.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  evaluateModelLoadPolicy,
  isNarrowViewport,
  isTouchPrimary,
  isIOS,
  isLowMemoryDevice,
  prefersReducedData,
  isFramed,
  isConstrainedIframe,
  isHighRiskObject,
  useModelLoadPolicy,
  HIGH_RISK_OBJECT_IDS,
  MOBILE_MAX_WIDTH,
  LOW_DEVICE_MEMORY_GB,
} from './modelLoadPolicy.js'

const NARROW = `(max-width: ${MOBILE_MAX_WIDTH}px)`
const TOUCH = '(hover: none) and (pointer: coarse)'
const REDUCED_DATA = '(prefers-reduced-data: reduce)'

const DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

function makeWindow({ trueQueries = [], framed = false, matchMediaThrows = false } = {}) {
  const set = new Set(trueQueries)
  const win = {
    matchMedia: matchMediaThrows
      ? () => { throw new Error('matchMedia unsupported') }
      : (query) => ({ matches: set.has(query) }),
  }
  win.self = win
  win.top = framed ? {} : win
  return win
}

function makeNavigator({
  userAgent = DESKTOP_UA,
  maxTouchPoints = 0,
  deviceMemory,
  connection,
  mozConnection,
  webkitConnection,
} = {}) {
  const nav = { userAgent, maxTouchPoints }
  if (deviceMemory !== undefined) nav.deviceMemory = deviceMemory
  if (connection !== undefined) nav.connection = connection
  if (mozConnection !== undefined) nav.mozConnection = mozConnection
  if (webkitConnection !== undefined) nav.webkitConnection = webkitConnection
  return nav
}

// Build an env + run the full policy. `win` and `nav` are option bags.
function policy({ objectId = '', win = {}, nav = {} } = {}) {
  return evaluateModelLoadPolicy({
    objectId,
    highRiskObjectIds: ['synthetic-large-model'],
    env: { window: makeWindow(win), navigator: makeNavigator(nav) },
  })
}

// ---- desktop / laptop ----
test('desktop/laptop auto-loads (no gate)', () => {
  const result = policy({ nav: { deviceMemory: 8 } })
  assert.equal(result.autoLoad, true)
  assert.equal(result.gated, false)
  assert.equal(result.highRisk, false)
  assert.deepEqual(result.reasons, [])
})

test('touchscreen laptop (fine pointer) auto-loads — coarse alone does not gate', () => {
  // hover:hover + pointer:fine => TOUCH query false; wide viewport.
  const result = policy({ nav: { deviceMemory: 8 } })
  assert.equal(result.device.touchPrimary, false)
  assert.equal(result.autoLoad, true)
})

// ---- narrow viewport ----
test('narrow mobile viewport gates', () => {
  const result = policy({ win: { trueQueries: [NARROW] }, nav: { deviceMemory: 8 } })
  assert.equal(result.gated, true)
  assert.equal(result.autoLoad, false)
  assert.ok(result.reasons.includes('narrow-viewport'))
})

// ---- touch-primary ----
test('touch-primary gates', () => {
  const result = policy({ win: { trueQueries: [TOUCH] }, nav: { deviceMemory: 8 } })
  assert.equal(result.gated, true)
  assert.ok(result.reasons.includes('touch-primary'))
})

// ---- iOS + iPadOS masquerade ----
for (const ua of ['iPhone', 'iPad', 'iPod touch']) {
  test(`iOS UA gates (${ua})`, () => {
    const result = policy({ nav: { userAgent: `Mozilla/5.0 (${ua})`, deviceMemory: 8 } })
    assert.equal(result.device.ios, true)
    assert.equal(result.gated, true)
    assert.ok(result.reasons.includes('ios'))
  })
}

test('iPadOS-13+ masquerade (Macintosh + maxTouchPoints>1) gates', () => {
  const result = policy({ nav: { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)', maxTouchPoints: 5, deviceMemory: 8 } })
  assert.equal(result.device.ios, true)
  assert.ok(result.reasons.includes('ios'))
})

test('real desktop Mac (Macintosh + maxTouchPoints 0) is NOT iOS and auto-loads', () => {
  const result = policy({ nav: { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)', maxTouchPoints: 0, deviceMemory: 8 } })
  assert.equal(result.device.ios, false)
  assert.equal(result.autoLoad, true)
})

// ---- deviceMemory ----
test('deviceMemory <= 4 gates (low-memory)', () => {
  const result = policy({ nav: { deviceMemory: LOW_DEVICE_MEMORY_GB } })
  assert.equal(result.device.lowMemory, true)
  assert.ok(result.reasons.includes('low-device-memory'))
})

test('deviceMemory 2 gates; deviceMemory 8 does not; absent does not', () => {
  assert.equal(policy({ nav: { deviceMemory: 2 } }).device.lowMemory, true)
  assert.equal(policy({ nav: { deviceMemory: 8 } }).device.lowMemory, false)
  assert.equal(policy({ nav: {} }).device.lowMemory, false)
  // Defensive: a bogus 0 must not count as "low" (memory > 0 required).
  assert.equal(policy({ nav: { deviceMemory: 0 } }).device.lowMemory, false)
})

// ---- saveData / prefers-reduced-data ----
test('Data Saver (connection.saveData) gates', () => {
  const result = policy({ nav: { deviceMemory: 8, connection: { saveData: true } } })
  assert.equal(result.device.saveData, true)
  assert.ok(result.reasons.includes('save-data'))
})

test('webkitConnection.saveData gates (vendor-prefixed)', () => {
  const result = policy({ nav: { deviceMemory: 8, webkitConnection: { saveData: true } } })
  assert.equal(result.device.saveData, true)
})

test('prefers-reduced-data media query gates when no connection API', () => {
  const result = policy({ win: { trueQueries: [REDUCED_DATA] }, nav: { deviceMemory: 8 } })
  assert.equal(result.device.saveData, true)
  assert.ok(result.reasons.includes('save-data'))
})

test('connection present with saveData false does not gate on its own', () => {
  const result = policy({ nav: { deviceMemory: 8, connection: { saveData: false } } })
  assert.equal(result.device.saveData, false)
  assert.equal(result.autoLoad, true)
})

// ---- iframe ----
test('constrained iframe (framed + narrow) gates', () => {
  const result = policy({ win: { framed: true, trueQueries: [NARROW] }, nav: { deviceMemory: 8 } })
  assert.equal(result.device.constrainedIframe, true)
  assert.ok(result.reasons.includes('constrained-iframe'))
})

test('constrained iframe (framed + touch-primary) gates', () => {
  const result = policy({ win: { framed: true, trueQueries: [TOUCH] }, nav: { deviceMemory: 8 } })
  assert.equal(result.device.constrainedIframe, true)
})

test('full-width desktop iframe auto-loads (framed but not small/touch)', () => {
  const result = policy({ win: { framed: true, trueQueries: [] }, nav: { deviceMemory: 8 } })
  assert.equal(isFramed({ window: makeWindow({ framed: true }) }), true)
  assert.equal(result.device.constrainedIframe, false)
  assert.equal(result.autoLoad, true)
  assert.deepEqual(result.reasons, [])
})

// ---- high-risk object ----
test('synthetic-large-model on iOS sets highRisk', () => {
  const result = policy({ objectId: 'synthetic-large-model', nav: { userAgent: 'iPhone', deviceMemory: 8 } })
  assert.equal(result.highRisk, true)
  assert.equal(result.gated, true)
  assert.equal(result.autoLoad, false)
})

test('high-risk id matching is trim/case-insensitive', () => {
  assert.equal(isHighRiskObject('  Synthetic-Large-Model ', ['synthetic-large-model']), true)
  const result = policy({ objectId: '  Synthetic-Large-Model ', nav: { userAgent: 'iPhone', deviceMemory: 8 } })
  assert.equal(result.highRisk, true)
})

test('high-risk object is NOT highRisk on non-iOS (still gated by viewport)', () => {
  const result = policy({ objectId: 'synthetic-large-model', win: { trueQueries: [NARROW] }, nav: { userAgent: DESKTOP_UA, deviceMemory: 8 } })
  assert.equal(result.gated, true)
  assert.equal(result.highRisk, false)
})

test('non-high-risk object on iOS is not highRisk', () => {
  const result = policy({ objectId: 'synthetic-vessel', nav: { userAgent: 'iPhone', deviceMemory: 8 } })
  assert.equal(result.device.ios, true)
  assert.equal(result.highRisk, false)
})

test('high-risk object on desktop never highRisk (auto-loads)', () => {
  const result = policy({ objectId: 'synthetic-large-model', nav: { userAgent: DESKTOP_UA, deviceMemory: 8 } })
  assert.equal(result.autoLoad, true)
  assert.equal(result.highRisk, false)
})

// ---- accumulation + shape ----
test('multiple signals accumulate reasons (iPhone narrow + touch + low memory + saveData)', () => {
  const result = policy({
    objectId: 'synthetic-large-model',
    win: { trueQueries: [NARROW, TOUCH, REDUCED_DATA] },
    nav: { userAgent: 'iPhone', maxTouchPoints: 5, deviceMemory: 2, connection: { saveData: true } },
  })
  for (const reason of ['narrow-viewport', 'touch-primary', 'ios', 'low-device-memory', 'save-data']) {
    assert.ok(result.reasons.includes(reason), `expected reason ${reason}`)
  }
  assert.equal(result.highRisk, true)
})

test('result shape is stable', () => {
  const result = policy({ nav: { deviceMemory: 8 } })
  assert.deepEqual(Object.keys(result).sort(), ['autoLoad', 'device', 'gated', 'highRisk', 'reasons'])
  assert.deepEqual(
    Object.keys(result.device).sort(),
    ['constrainedIframe', 'ios', 'lowMemory', 'narrowViewport', 'saveData', 'touchPrimary'],
  )
  assert.ok(Array.isArray(result.reasons))
})

// ---- detector edge branches ----
test('isNarrowViewport falls back to innerWidth when matchMedia is absent', () => {
  assert.equal(isNarrowViewport({ window: { innerWidth: 500 } }), true)
  assert.equal(isNarrowViewport({ window: { innerWidth: 1200 } }), false)
})

test('matchMedia exceptions are swallowed (treated as non-match)', () => {
  const env = { window: makeWindow({ matchMediaThrows: true }), navigator: makeNavigator({}) }
  assert.equal(isTouchPrimary(env), false)
})

test('isFramed returns true when window.top access throws (cross-origin)', () => {
  const win = { self: {} }
  Object.defineProperty(win, 'top', { get() { throw new Error('cross-origin') } })
  assert.equal(isFramed({ window: win }), true)
})

test('SSR / no-window+no-navigator is safe and auto-loads', () => {
  const result = evaluateModelLoadPolicy({ env: { window: undefined, navigator: undefined } })
  assert.equal(result.autoLoad, true)
  assert.equal(result.gated, false)
  assert.equal(typeof result.autoLoad, 'boolean')
})

// ---- no ios-low-memory variant anywhere ----
test('no policy path produces an ios-low-memory variant token', () => {
  const matrix = [
    policy({ nav: { userAgent: 'iPhone' } }),
    policy({ objectId: 'synthetic-large-model', nav: { userAgent: 'iPhone' } }),
    policy({ win: { trueQueries: [NARROW] } }),
    policy({ nav: { deviceMemory: 2 } }),
    policy({ win: { framed: true, trueQueries: [TOUCH] } }),
    policy({ nav: { deviceMemory: 8 } }),
  ]
  for (const result of matrix) {
    const blob = JSON.stringify(result)
    assert.ok(!/ios-low-memory/i.test(blob), `unexpected variant token in ${blob}`)
    assert.ok(!/variant=ios/i.test(blob), `unexpected variant query in ${blob}`)
    for (const reason of result.reasons) {
      assert.ok(!/ios-low-memory|variant=ios/i.test(reason))
    }
  }
})

test('policy module source contains no ios-low-memory variant reference', () => {
  const source = readFileSync(new URL('./modelLoadPolicy.js', import.meta.url), 'utf8')
  assert.ok(!/ios-low-memory/i.test(source), 'module must not reference the ios-low-memory variant')
  assert.ok(!/variant=ios/i.test(source))
  assert.ok(!/\.glb/i.test(source), 'policy must not build model URLs')
})

test('HIGH_RISK_OBJECT_IDS is frozen and empty by default', () => {
  assert.ok(Object.isFrozen(HIGH_RISK_OBJECT_IDS))
  assert.deepEqual(HIGH_RISK_OBJECT_IDS, [])
})

// ---- useModelLoadPolicy (initial-state derivation via real React render) ----
function renderHook(objectId, win) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  globalThis.window = win
  try {
    const captured = {}
    function Probe() {
      captured.value = useModelLoadPolicy(objectId)
      return null
    }
    renderToStaticMarkup(React.createElement(Probe))
    return captured.value
  } finally {
    if (previous) {
      Object.defineProperty(globalThis, 'window', previous)
    } else {
      delete globalThis.window
    }
  }
}

test('useModelLoadPolicy: desktop window -> autoLoad', () => {
  const result = renderHook('', makeWindow({ trueQueries: [] }))
  assert.equal(result.autoLoad, true)
  assert.equal(result.gated, false)
})

test('useModelLoadPolicy: narrow window -> gated with reason', () => {
  const result = renderHook('', makeWindow({ trueQueries: [NARROW] }))
  assert.equal(result.gated, true)
  assert.ok(result.reasons.includes('narrow-viewport'))
})

test('useModelLoadPolicy: returns a well-formed policy object', () => {
  const result = renderHook('synthetic-vessel', makeWindow({ trueQueries: [] }))
  assert.deepEqual(Object.keys(result).sort(), ['autoLoad', 'device', 'gated', 'highRisk', 'reasons'])
})
