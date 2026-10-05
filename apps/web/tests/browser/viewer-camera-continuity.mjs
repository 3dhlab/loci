// Actual local seeded demo: drag -> direct pin keeps camera; rail/link still frame.
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

const origin = process.env.DEMO_WEB_URL || 'http://127.0.0.1:8080'
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname), 'Local demo only')
const artifacts = resolve(process.env.PLAYBACK_ARTIFACTS || 'test-results/viewer-camera-continuity')
mkdirSync(artifacts, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })
const report = { origin, api: 'actual seeded demo; no request interception', cases: [] }
const poseDistance = (a, b) => Math.max(...['position', 'target'].flatMap(key => a[key].map((value, i) => Math.abs(value - b[key][i]))))
async function settledCamera(page) {
  // OrbitControls damping is frame based. A fixed wall-clock delay can still
  // sample a moving drag on a busy software-rendered CI worker.
  await page.waitForFunction(() => {
    const pose = window.cameraContinuityViews.at(-1)
    if (!pose) return false
    const sample = window.cameraSettleSample
    const time = performance.now()
    const distance = sample ? Math.max(...['position', 'target'].flatMap(key =>
      pose[key].map((value, i) => Math.abs(value - sample.pose[key][i])))) : Infinity
    const stableSince = distance < .0001 ? sample.stableSince : time
    window.cameraSettleSample = { pose, stableSince }
    return time - stableSince >= 500
  }, null, { timeout: 10000, polling: 100 })
  await page.evaluate(() => { window.cameraSettleSample = null })
  return page.evaluate(() => window.cameraContinuityViews.at(-1))
}
try {
  for (const reducedMotion of ['reduce', 'no-preference']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion })
    const page = await context.newPage()
    page.setDefaultTimeout(25000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => {
      window.cameraContinuityViews = []
      document.addEventListener('model-canvas-camera-view-change', event => {
        window.cameraContinuityViews.push(event.detail)
      })
    })
    await page.goto(`${origin}/evidence/objects/demo-cube`, { waitUntil: 'domcontentloaded' })
    const stage = page.locator('.model-canvas-shell')
    const pin = stage.getByRole('button', { name: 'Compare two sections', exact: true })
    await pin.waitFor()
    await page.waitForFunction(() => window.cameraContinuityViews.length > 0)
    const initialPose = await page.evaluate(() => window.cameraContinuityViews.at(-1))
    const box = await stage.locator('canvas').boundingBox()
    assert.ok(box)
    await page.mouse.move(box.x + box.width * .82, box.y + box.height * .82)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * .82 - 170, box.y + box.height * .82 - 50, { steps: 30 })
    await page.mouse.up()
    const manualPose = await settledCamera(page)
    assert.ok(poseDistance(initialPose, manualPose) > .1, 'The real drag must substantially change the camera')

    // A single interior window must retain focus after its completion seek.
    // Top edge is the 4–8s window in the real seeded demo.
    const singlePin = stage.getByRole('button', { name: 'Top edge', exact: true })
    const singleClip = page.getByRole('button', { name: /00:04.*00:08/ })
    await singlePin.click()
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && !media.seeking && media.paused && media.currentTime >= 7.9 && media.currentTime < 8
    }, null, { timeout: 20000 })
    await page.waitForTimeout(500)
    const singleStopSeconds = await page.locator('video.evidence-video-element').evaluate(node => node.currentTime)
    assert.ok(singleStopSeconds < 8, 'The final frame stays inside the exclusive 4–8s window')
    assert.equal(await singlePin.getAttribute('aria-current'), 'true',
      `completed single window retains its selected pin (${reducedMotion})`)
    assert.equal(await singleClip.getAttribute('aria-pressed'), 'true',
      `completed single window retains its active clip (${reducedMotion})`)
    const singleCompletionPose = await page.evaluate(() => window.cameraContinuityViews.at(-1))
    assert.ok(poseDistance(manualPose, singleCompletionPose) < .005,
      `single-window completion preserves the camera (${reducedMotion}): ${poseDistance(manualPose, singleCompletionPose)}`)
    // A later seek to a distinct timestamp remains a manual scrub and clears focus.
    await page.locator('video.evidence-video-element').evaluate(node => { node.currentTime = 6.5 })
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && !media.seeking && Math.abs(media.currentTime - 6.5) < .1
    })
    await page.waitForFunction(() =>
      document.querySelector('.model-canvas-shell button[aria-current="true"]') === null)
    assert.notEqual(await singlePin.getAttribute('aria-current'), 'true',
      'manual scrub clears the selected pin after guided completion')
    assert.equal(await page.evaluate(() =>
      document.querySelector('.studio-clip-sequence button[aria-pressed="true"]') === null), true,
    'manual scrub clears the active clip after guided completion')

    // Manual scrubbing resets camera intent, so establish a fresh user pose for
    // the separate two-window continuity check.
    const poseAfterManualScrub = await page.evaluate(() => window.cameraContinuityViews.at(-1))
    const postScrubBox = await stage.locator('canvas').boundingBox()
    assert.ok(postScrubBox)
    await page.mouse.move(postScrubBox.x + postScrubBox.width * .82, postScrubBox.y + postScrubBox.height * .82)
    await page.mouse.down()
    await page.mouse.move(postScrubBox.x + postScrubBox.width * .82 - 150, postScrubBox.y + postScrubBox.height * .82 - 45, { steps: 30 })
    await page.mouse.up()
    const sequenceManualPose = await settledCamera(page)
    assert.ok(poseDistance(poseAfterManualScrub, sequenceManualPose) > .1,
      'A new camera drag establishes the two-window continuity baseline')

    const eventStart = await page.evaluate(() => window.cameraContinuityViews.length)
    await pin.click()
    await page.waitForFunction(() => document.querySelector('video.evidence-video-element')?.currentTime > .05)
    assert.equal(await pin.getAttribute('aria-current'), 'true')
    await page.locator('video.evidence-video-element').evaluate(node => {
      window.guidedSequenceTimes = [node.currentTime]
      for (const event of ['timeupdate', 'seeking', 'seeked']) {
        node.addEventListener(event, () => window.guidedSequenceTimes.push(node.currentTime))
      }
    })
    const secondClip = page.getByRole('button', { name: /Clip 2.*00:08.*00:12/ })
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      const active = [...document.querySelectorAll('.studio-clip-sequence button')]
        .find(button => button.textContent.includes('Clip 2'))
      return media && !media.paused && media.currentTime >= 8 && media.currentTime < 12
        && active?.getAttribute('aria-pressed') === 'true'
    }, null, { timeout: 12000 })
    assert.equal(await secondClip.getAttribute('aria-pressed'), 'true',
      `two-window sequence advances to its second clip (${reducedMotion})`)
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && media.paused && media.currentTime >= 11.9 && media.currentTime < 12.2
    }, null, { timeout: 12000 })
    assert.equal(await pin.getAttribute('aria-current'), 'true',
      `completed two-window sequence retains its selected pin (${reducedMotion})`)
    assert.equal(await secondClip.getAttribute('aria-pressed'), 'true',
      `completed two-window sequence retains its final active clip (${reducedMotion})`)
    const sequenceTimes = await page.evaluate(() => window.guidedSequenceTimes)
    assert.ok(sequenceTimes.some((time, index) => index > 0
      && sequenceTimes[index - 1] < 4.5 && time >= 8),
    `two-window sequence skips the gap between clips: ${JSON.stringify(sequenceTimes)}`)
    const subsequent = await page.evaluate(start => window.cameraContinuityViews.slice(start), eventStart)
    const largestDirectSelectionChange = Math.max(0, ...subsequent.map(pose => poseDistance(sequenceManualPose, pose)))
    assert.ok(largestDirectSelectionChange < .005,
      `Direct pin must preserve the manually chosen camera (${reducedMotion}): ${largestDirectSelectionChange}`)
    assert.deepEqual(errors, [])
    await page.screenshot({ path: resolve(artifacts, `direct-pin-${reducedMotion}.png`) })
    // Re-select the SAME annotation through the rail: an intentional navigation
    // must request framing even though its annotation ID has not changed.
    await page.locator('.studio-rail-item').filter({ hasText: 'Compare two sections' }).click()
    await page.waitForFunction(({ pose }) => {
      const current = window.cameraContinuityViews.at(-1)
      return current && ['position', 'target'].some(key => current[key].some((v, i) => Math.abs(v - pose[key][i]) > .05))
    }, { pose: sequenceManualPose })
    const railPose = await page.evaluate(() => window.cameraContinuityViews.at(-1))
    // Scrubbing during a live guided window disarms its boundary enforcement.
    const guidedSinglePin = stage.getByRole('button', { name: 'Top edge', exact: true })
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      const secondClip = [...document.querySelectorAll('.studio-clip-sequence button')]
        .find(button => button.textContent.includes('Clip 2'))
      return media && !media.paused && media.currentTime >= 10 && media.currentTime < 11.5
        && secondClip?.getAttribute('aria-pressed') === 'true'
    }, null, { timeout: 15000 })
    await guidedSinglePin.click()
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      const activeClip = document.querySelector('.studio-clip-sequence button[aria-pressed="true"]')
      const selectedPin = [...document.querySelectorAll('.model-canvas-shell button[aria-current="true"]')]
        .find(button => button.textContent.includes('Top edge'))
      return media && !media.paused && !media.seeking && media.readyState >= 2
        && media.currentTime > 4.2 && media.currentTime < 7.8
        && activeClip?.textContent.includes('00:04') && activeClip.textContent.includes('00:08')
        && selectedPin
    }, null, { timeout: 10000 })
    assert.equal(await guidedSinglePin.getAttribute('aria-current'), 'true',
      'switching ranges during playback keeps the newly selected pin active')
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && !media.paused && media.currentTime >= 4.5 && media.currentTime < 5.8
    }, null, { timeout: 5000 })
    await page.locator('video.evidence-video-element').evaluate(node => { node.currentTime = 6.5 })
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && !media.seeking && Math.abs(media.currentTime - 6.5) < .1
        && document.querySelector('.model-canvas-shell button[aria-current="true"]') === null
        && document.querySelector('.studio-clip-sequence button[aria-pressed="true"]') === null
    })
    await page.waitForFunction(() => {
      const media = document.querySelector('video.evidence-video-element')
      return media && !media.paused && media.currentTime >= 8.2 && media.currentTime < 9.5
    }, null, { timeout: 5000 })
    await page.goto(`${origin}/evidence/objects/demo-cube?annotation=demo-annotation-3`, { waitUntil: 'domcontentloaded' })
    const restoredPin = page.locator('.model-canvas-shell').getByRole('button', { name: 'Compare two sections', exact: true })
    await restoredPin.waitFor()
    await page.waitForFunction(() => document.querySelector('.model-canvas-shell button[aria-current="true"]'))
    await page.waitForFunction(() => window.cameraContinuityViews.length > 0)
    assert.equal(await restoredPin.getAttribute('aria-current'), 'true')
    assert.deepEqual(errors, [])
    report.cases.push({ reducedMotion, dragChange: poseDistance(initialPose, manualPose),
      singleStopSeconds,
      singleCompletionPoseChange: poseDistance(manualPose, singleCompletionPose),
      manualScrubClearedPin: true, guidedScrubClearedFocusAndContinued: true,
      sequenceTimes, largestDirectSelectionChange,
      railChange: poseDistance(sequenceManualPose, railPose), restoredAnnotation: true })
    await context.close()
  }
  console.log(JSON.stringify(report))
} catch (error) {
  report.error = error.stack
  throw error
} finally {
  writeFileSync(resolve(artifacts, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  await browser.close()
}
