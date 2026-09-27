import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

const origin = process.env.DEMO_WEB_URL || 'http://127.0.0.1:8080'
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname), 'Demo checks use loopback only')
const artifacts = resolve('test-results/demo-journey')
mkdirSync(artifacts, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })
const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(20000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
const report = { apiMode: 'actual seeded API and database; no route interception', origin }
try {
  await context.tracing.start({ screenshots: true, snapshots: true })
  await page.goto(`${origin}/public`, { waitUntil: 'domcontentloaded' })
  const objectLink = page.getByRole('link', { name: /Demo cube/ }).first()
  await objectLink.waitFor()
  await objectLink.click()
  await page.getByText('Demo cube', { exact: true }).first().waitFor()
  const marker = page.getByRole('button', { name: 'Compare two sections', exact: true }).first()
  await marker.waitFor()
  await marker.click()
  const video = page.locator('video.evidence-video-element')
  await video.waitFor({ state: 'attached' })
  await page.waitForFunction(() => document.querySelector('video.evidence-video-element')?.readyState >= 2)
  const firstClip = page.getByRole('button', { name: /Clip 1.*00:00.*00:04/ })
  const secondClip = page.getByRole('button', { name: /Clip 2.*00:08.*00:12/ })
  assert.equal(await firstClip.getAttribute('aria-pressed'), 'true', 'annotation starts with the blue clip selected')
  assert.equal(await marker.getAttribute('aria-current'), 'true', 'annotation selection is reflected on its spatial marker')
  const initialTime = await video.evaluate(node => {
    window.demoPlaybackTimes = [node.currentTime]
    for (const event of ['timeupdate', 'seeking', 'seeked']) {
      node.addEventListener(event, () => window.demoPlaybackTimes.push(node.currentTime))
    }
    return node.currentTime
  })
  assert.ok(initialTime >= 0 && initialTime < 4, `first clip must start in the blue window, received ${initialTime}s`)
  await video.evaluate(node => node.play())
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    const selected = [...document.querySelectorAll('.studio-clip-sequence button')]
      .find(button => button.textContent.includes('Clip 2'))
    return node && !node.paused && !node.seeking && node.currentTime >= 8 && node.currentTime < 12
      && selected?.getAttribute('aria-pressed') === 'true'
  }, null, { timeout: 15000 })
  const playbackTimes = await page.evaluate(() => window.demoPlaybackTimes)
  assert.ok(playbackTimes.some((time, index) => index > 0 && playbackTimes[index - 1] < 4.5 && time >= 8),
    `guided playback must jump across the amber gap: ${JSON.stringify(playbackTimes)}`)
  assert.equal(await firstClip.getAttribute('aria-pressed'), 'false')
  assert.equal(await secondClip.getAttribute('aria-pressed'), 'true')
  // Explicit clip selection gives the share action a clip focus; the automatic
  // sequence retains its annotation focus and shares the complete annotation.
  await secondClip.click()
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.seeking && Math.abs(node.currentTime - 8) <= 0.35
  })
  await page.getByRole('button', { name: 'Copy link', exact: true }).click()
  await page.getByText('Link copied.', { exact: true }).waitFor()
  const share = await page.evaluate(() => navigator.clipboard.readText())
  const url = new URL(share)
  assert.equal(url.pathname, '/evidence/objects/demo-cube')
  assert.equal(url.searchParams.get('clip'), 'demo-clip-3', 'share retains the purple clip')
  assert.equal(url.searchParams.get('annotation_context'), 'demo-annotation-3', 'share retains the selected annotation')
  await page.goto(share, { waitUntil: 'domcontentloaded' })
  await page.getByText('Demo cube', { exact: true }).first().waitFor()
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.seeking && node.readyState >= 2 && Math.abs(node.currentTime - 8) <= 0.35
  }, null, { timeout: 15000 })
  assert.equal(await marker.getAttribute('aria-current'), 'true', 'share reload restores the spatial annotation')
  assert.equal(await secondClip.getAttribute('aria-pressed'), 'true', 'share reload restores the linked purple clip')
  report.clipTransition = { from: 'demo-clip-1', to: 'demo-clip-3', playbackTimes }
  report.restoredTimeMs = await video.evaluate(node => node.currentTime * 1000)
  await page.getByRole('button', { name: 'Copy citation', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Copy citation' })
  await dialog.waitFor()
  assert.match(await dialog.locator('textarea').inputValue(), /Demo cube/)
  await page.screenshot({ path: resolve(artifacts, 'demo.png'), fullPage: true })
  assert.deepEqual(errors, [])
  report.result = 'PASS'
  report.sharePath = url.pathname + url.search
  console.log(JSON.stringify(report, null, 2))
} catch (error) {
  report.result = 'FAIL'
  report.error = error.stack
  report.pageErrors = errors
  console.error(JSON.stringify(report, null, 2))
  process.exitCode = 1
} finally {
  writeFileSync(resolve(artifacts, 'summary.json'), JSON.stringify(report, null, 2) + '\n')
  await context.tracing.stop({ path: resolve(artifacts, 'trace.zip') })
  await browser.close()
}
