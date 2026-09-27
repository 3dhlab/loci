/* Real-browser smoke for the adaptive 3D load policy.
 *
 * The node:test suite (src/lib/modelLoadPolicy.test.js) covers every branch with
 * injected envs. This smoke complements it by exercising the SAME policy core
 * (which useModelLoadPolicy delegates to) against a REAL browser environment —
 * real navigator UA, real matchMedia, real iframe check — in a desktop context
 * vs an iPhone 13 (iOS) context.
 *
 * Requires a running Vite dev server that serves the source module. Set BASE
 * (default http://127.0.0.1:5173). webkit only — it creates a WebGL/real iOS UA
 * context; headless chromium cannot create a WebGL context in CI here.
 *
 * Usage: BASE=http://127.0.0.1:5173 node scripts/modelLoadPolicySmoke.cjs
 */
const { webkit, devices } = require('playwright')

const BASE = process.env.BASE || 'http://127.0.0.1:5173'
const MODULE_PATH = '/src/lib/modelLoadPolicy.js'

const results = []
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail || '' })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`)
}

async function evalPolicy(contextOpts, objectId) {
  const browser = await webkit.launch({ headless: true })
  const ctx = await browser.newContext(contextOpts)
  const page = await ctx.newPage()
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 })
    return await page.evaluate(async ({ modulePath, id }) => {
      const mod = await import(modulePath)
      // Calls the real-env path (no injected env) — exactly what the hook uses.
      return mod.evaluateModelLoadPolicy({ objectId: id, highRiskObjectIds: ['synthetic-large-model'] })
    }, { modulePath: MODULE_PATH, id: objectId })
  } finally {
    await browser.close()
  }
}

async function main() {
  // Desktop: real fine pointer, wide viewport, not iOS, not framed -> auto-load.
  const desktop = await evalPolicy({ viewport: { width: 1440, height: 900 } }, 'synthetic-vessel')
  check('desktop (real browser): auto-loads', desktop.autoLoad === true, JSON.stringify(desktop.reasons))
  check('desktop (real browser): no ios-low-memory token', !/ios-low-memory|variant=ios/i.test(JSON.stringify(desktop)))

  // iPhone 13 (iOS): real iOS UA + narrow + touch -> gated.
  const mobile = await evalPolicy({ ...devices['iPhone 13'] }, 'synthetic-vessel')
  check('iPhone (real browser): gated', mobile.gated === true, JSON.stringify(mobile.reasons))
  check('iPhone (real browser): reason includes ios', mobile.reasons.includes('ios'), JSON.stringify(mobile.reasons))
  check('iPhone (real browser): reason includes narrow-viewport', mobile.reasons.includes('narrow-viewport'))
  check('iPhone (real browser): no ios-low-memory token', !/ios-low-memory|variant=ios/i.test(JSON.stringify(mobile)))

  // iPhone + high-risk object -> highRisk.
  const highRisk = await evalPolicy({ ...devices['iPhone 13'] }, 'synthetic-large-model')
  check('iPhone (real browser): horizontal-mask is highRisk', highRisk.highRisk === true, JSON.stringify(highRisk.reasons))
  check('iPhone (real browser): high-risk never auto-loads', highRisk.autoLoad === false)

  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} smoke checks passed`)
  if (failed.length) {
    console.log('FAILED: ' + failed.map((f) => f.name).join('; '))
    process.exit(1)
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
