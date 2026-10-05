// The supported single-range tutorial, exercised against a disposable real demo.
import assert from 'node:assert/strict'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

const origin = process.env.DEMO_WEB_URL || 'http://127.0.0.1:8080'
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname), 'Local demo only')
const repoRoot = resolve(import.meta.dirname, '../../../..')
const values = Object.fromEntries(readFileSync(resolve(repoRoot, '.env'), 'utf8').split('\n')
  .filter(line => line && !line.startsWith('#')).map(line => {
    const separator = line.indexOf('=')
    return [line.slice(0, separator), line.slice(separator + 1)]
  }))
assert.ok(values.DEMO_PASSWORD?.length >= 20, 'Use the generated disposable demo credentials')
const artifacts = resolve('test-results/demo-authoring')
mkdirSync(artifacts, { recursive: true })
const browser = await chromium.launch({ headless: true,
  args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(25000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
let token, createdId
const report = { api: 'actual seeded API and database; no request interception', origin }
async function publicPreview() {
  const response = await context.request.get(`${origin}/api/v1/public/objects/00000000-0000-4000-8000-000000000003/preview`)
  assert.ok(response.ok(), `Synthetic public preview: ${response.status()}`)
  return response.json()
}
try {
  const initial = await publicPreview()
  assert.equal(initial.video.id, '00000000-0000-4000-8000-000000000005', 'Only the generated demo')
  const originalIds = initial.annotations.map(annotation => annotation.id).sort()
  await page.goto(`${origin}/console`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('Email', { exact: true }).fill('demo@example.org')
  await page.getByLabel('Password', { exact: true }).fill(values.DEMO_PASSWORD)
  await page.getByRole('button', { name: 'Open Loci Console', exact: true }).click()
  const clipPanel = page.locator('.clip-export-panel')
  await clipPanel.waitFor()
  token = await page.evaluate(() => localStorage.getItem('semantic.console.token'))
  assert.ok(token, 'The real login completed')
  // Never collect a login trace, storage state, credentials or authenticated URLs.
  await page.waitForFunction(() => document.querySelector('.viewer-video video')?.readyState >= 2)
  await clipPanel.getByLabel('Start ms', { exact: true }).fill('0')
  await clipPanel.getByLabel('End ms', { exact: true }).fill('4000')
  await clipPanel.getByRole('button', { name: 'Map to 3D', exact: true }).click()
  const editor = page.locator('.model-side-card').filter({ has: page.getByRole('heading', { name: 'Annotation Editor', exact: true }) })
  await editor.waitFor()
  assert.equal(await editor.getByLabel('Annotation start ms').inputValue(), '0')
  assert.equal(await editor.getByLabel('Annotation end ms').inputValue(), '4000')
  const title = `Synthetic authoring check ${Date.now()}`
  await editor.getByLabel('Annotation title', { exact: true }).fill(title)
  let annotationPosts = 0
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/model/annotations')) annotationPosts += 1
  })
  await editor.getByRole('button', { name: 'Create annotation', exact: true }).click()
  await page.getByText('Click the model to capture a valid annotation point before saving.', { exact: true }).waitFor()
  assert.equal(annotationPosts, 0, 'An untouched draft never reaches the API')
  await editor.getByRole('button', { name: 'Place new pin for this range', exact: true }).click()
  const stage = page.locator('.model-canvas-panel .model-canvas-shell').first()
  await stage.getByRole('button', { name: 'Front face', exact: true }).waitFor()
  const canvas = stage.locator('canvas')
  const box = await canvas.boundingBox()
  assert.ok(box)
  await canvas.click({ position: { x: box.width * .5, y: box.height * .54 } })
  await editor.getByText('Captured', { exact: true }).waitFor()
  const creation = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/model/annotations'))
  await editor.getByRole('button', { name: 'Create annotation', exact: true }).click()
  const response = await creation
  assert.ok(response.ok(), `Create annotation: ${response.status()}`)
  const created = await response.json()
  createdId = created.id
  assert.equal(created.is_published, false)
  const point = [created.point_x, created.point_y, created.point_z]
  assert.ok(point.every(Number.isFinite))
  assert.ok(point.some(value => Math.abs(Math.abs(value) - .5) < .001), 'The pin lies on the cube surface')
  assert.equal((await publicPreview()).annotations.some(annotation => annotation.id === createdId), false,
    'A private annotation is absent from the public projection')
  const row = page.locator('.model-annotation-item').filter({ hasText: title })
  await row.getByRole('button', { name: 'Preview', exact: true }).click()
  const overlay = page.locator('.annotation-overlay-shell')
  await overlay.getByRole('heading', { name: title, exact: true }).waitFor()
  await page.waitForFunction(() => document.querySelector('video.annotation-overlay-video')?.readyState >= 2)
  const previewSeconds = await overlay.locator('video').evaluate(node => {
    node.pause()
    return node.currentTime
  })
  assert.ok(previewSeconds >= 0 && previewSeconds < 4, 'Private playback starts inside the mapped range')
  await page.screenshot({ path: resolve(artifacts, 'private-preview.png') })
  await overlay.getByRole('button', { name: 'Close', exact: true }).click()
  await row.getByRole('button', { name: 'Publish', exact: true }).click()
  await row.getByRole('button', { name: 'Unpublish', exact: true }).waitFor()
  const published = (await publicPreview()).annotations.find(annotation => annotation.id === createdId)
  assert.ok(published?.evidence_url, 'Individual publication creates a canonical evidence link')
  const publicPage = await context.newPage()
  publicPage.setDefaultTimeout(25000)
  publicPage.on('pageerror', error => errors.push(error.message))
  await publicPage.goto(new URL(published.evidence_url, origin).href)
  const pin = publicPage.locator('.model-canvas-shell').getByRole('button', { name: title, exact: true })
  await pin.waitFor()
  await pin.click()
  await publicPage.waitForFunction(() => {
    const video = document.querySelector('video.evidence-video-element')
    return video && !video.seeking && video.paused && video.currentTime >= 3.8 && video.currentTime < 4
  }, null, { timeout: 15000 })
  const stoppedSeconds = await publicPage.locator('video.evidence-video-element').evaluate(node => node.currentTime)
  assert.equal(await pin.getAttribute('aria-current'), 'true', 'Playback completion retains the selected pin')
  await publicPage.screenshot({ path: resolve(artifacts, 'published-range.png') })
  // Clean up through the same authoring UI used by the tutorial.
  page.once('dialog', dialog => dialog.accept())
  await row.getByRole('button', { name: 'Delete', exact: true }).click()
  await row.waitFor({ state: 'detached' })
  assert.deepEqual((await publicPreview()).annotations.map(annotation => annotation.id).sort(), originalIds,
    'Cleanup retains the original public sample annotations')
  createdId = null
  assert.deepEqual(errors, [])
  Object.assign(report, { result: 'PASS', blankPlacementRejected: true, privateProjectionAbsent: true,
    capturedPoint: point, previewSeconds, stoppedSeconds, individualPublication: true, cleanedUp: true })
} catch (error) {
  report.result = 'FAIL'
  report.error = error.stack
  report.pageErrors = errors
  process.exitCode = 1
} finally {
  if (createdId && token) {
    const response = await context.request.delete(`${origin}/api/v1/objects/model-annotations/${createdId}`,
      { headers: { Authorization: `Bearer ${token}` } })
    report.failureCleanup = response.ok() || response.status() === 404
  }
  await browser.close()
  writeFileSync(resolve(artifacts, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify(report, null, 2))
}
