import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = resolve(fileURLToPath(new URL('.', import.meta.url)))
const webRoot = resolve(here, '../..')
const distRoot = resolve(webRoot, 'dist-ci')
const mediaPath = resolve(here, '../fixtures/synthetic-12s.mp4')
const modelPath = resolve(here, '../fixtures/synthetic-cube.glb')
const posterPath = resolve(here, '../fixtures/synthetic-poster.png')
const expectedSha256 = 'ad2409b6c2c734caceb0847f0d02215e285b5cf8a2cf76a57e6ec9e7c353f2e6'
const expectedModelSha256 = 'ca427dd09cdabf6dec4779683a5525dba14a21047c4c8929451c5f90ebb0b17a'
const expectedPosterSha256 = '5d33e5f3b13457707428c8dd083307ff283156b292f65548e7df802781d2bdaf'
const actualSha256 = createHash('sha256').update(readFileSync(mediaPath)).digest('hex')
const actualModelSha256 = createHash('sha256').update(readFileSync(modelPath)).digest('hex')
const actualPosterSha256 = createHash('sha256').update(readFileSync(posterPath)).digest('hex')
assert.equal(actualSha256, expectedSha256, 'synthetic browser media checksum changed')
assert.equal(actualModelSha256, expectedModelSha256, 'synthetic browser model checksum changed')
assert.equal(actualPosterSha256, expectedPosterSha256, 'synthetic browser poster checksum changed')
assert.ok(existsSync(resolve(distRoot, 'index.html')), 'run the CI production build before the browser regression')

const objectSlug = 'synthetic-object'
const annotationId = 'synthetic-annotation-01'
const overlappingAnnotationId = 'synthetic-annotation-00'
const firstClip = { id: 'synthetic-clip-a', title: 'First linked window', start_ms: 2000, end_ms: 4000 }
const secondClip = { id: 'synthetic-clip-b', title: 'Second linked window', start_ms: 6000, end_ms: 9000 }
const transcriptSegments = Array.from({ length: 48 }, (_, index) => {
  const startMs = index * 250
  const text = startMs === 6000
    ? 'Synthetic segment two, aligned to the second clip.'
    : startMs === 3000
      ? 'Later line at three seconds inside the annotation window.'
      : startMs === 3500
        ? 'Later line at three and a half seconds inside the annotation window.'
        : startMs === 2000
          ? 'Synthetic segment one, aligned to the first clip.'
          : `Synthetic transcript line ${index + 1} for scroll tracking.`
  return {
    position: index,
    start_ms: startMs,
    end_ms: startMs + 250,
    text,
  }
})
const privateFields = ['raw_text_observed', 'presenter_name_raw', 'raw_payload_json', 'source_video_path', 'private_author_notes']
const failures = []
const consoleMessages = []
const requestDiagnostics = []
const apiDiagnostics = []
let browserOrigin = ''
const fixture = (url, requestParams = {}) => {
  const requestedClipId = requestParams.clip || requestParams.clip_id || ''
  const requestedAnnotationId = requestParams.annotation || requestParams.annotation_id || ''
  const activeClip = requestedClipId === secondClip.id ? secondClip : firstClip
  const isClipFocus = requestedClipId === activeClip.id
  const isAnnotationFocus = requestedAnnotationId === annotationId || requestedAnnotationId === overlappingAnnotationId
  const initialSeek = isClipFocus ? activeClip.start_ms : isAnnotationFocus ? firstClip.start_ms : 0
  const annotation = { id: annotationId, title: 'Vessel rim annotation', description: 'A generated spatial note.', start_ms: 2000, end_ms: 9000, point_x: 0, point_y: 0, point_z: 1, normal_x: 0, normal_y: 0, normal_z: 1, related_clip_ids: [firstClip.id, secondClip.id] }
  const overlappingAnnotation = { id: overlappingAnnotationId, title: 'Synthetic body annotation', description: 'A second generated spatial note linked to the same clips.', start_ms: 2000, end_ms: 9000, point_x: -0.65, point_y: 0.35, point_z: 1, normal_x: 0, normal_y: 0, normal_z: 1, related_clip_ids: [firstClip.id, secondClip.id] }
  return {
    canonical_url: `http://127.0.0.1:${server.address().port}/evidence/objects/${objectSlug}${isClipFocus ? `?clip=${activeClip.id}` : isAnnotationFocus ? `?annotation=${annotationId}` : ''}`,
    page: { title: 'Synthetic annotation: vessel rim', summary: 'Generated browser regression fixture.' },
    object: { id: 'synthetic-object-id', title: 'Synthetic Vessel', summary: 'Generated locally for CI.', external_url: null, project_slug: 'synthetic-collection', project_name: 'Synthetic Collection' },
    focus: { source: isClipFocus ? 'clip' : isAnnotationFocus ? 'annotation' : 'default', object_id: 'synthetic-object-id', annotation_id: isAnnotationFocus ? annotationId : null, clip_id: isClipFocus ? activeClip.id : null, video_id: 'synthetic-video-a', seek_ms: initialSeek, start_ms: isClipFocus || isAnnotationFocus ? activeClip.start_ms : 0, end_ms: isClipFocus || isAnnotationFocus ? activeClip.end_ms : 12000 },
    model: { model_url: `${browserOrigin}/fixtures/synthetic-cube.glb`, default_camera: null, selected_camera: null },
    playback: { video_id: 'synthetic-video-a', video_title: 'Synthetic camera pan', video_stream_url: `${browserOrigin}/fixtures/synthetic-12s.mp4`, seek_ms: initialSeek, window_start_ms: firstClip.start_ms, window_end_ms: secondClip.end_ms, duration_ms: 12000 },
    selected_annotation: isClipFocus ? null : isAnnotationFocus ? (requestedAnnotationId === overlappingAnnotationId ? overlappingAnnotation : annotation) : null,
    selected_clip: null,
    annotations: [annotation, overlappingAnnotation],
    clips: [firstClip, secondClip],
    transcript: { video_id: 'synthetic-video-a', segments: transcriptSegments },
    citation_attribution: { speaker_label: 'Synthetic Speaker', session_date: '2026-01-02', session_date_text: 'January 2, 2026', session_date_precision: 'day', attribution_mode: 'named' },
    citation_attribution_timeline: [{ video_id: 'synthetic-video-a', start_ms: 0, end_ms: 12000, speaker_label: 'Synthetic Speaker', session_date: '2026-01-02', session_date_text: 'January 2, 2026' }],
  }
}

const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.mp4': 'video/mp4' }
const server = createServer((req, res) => {
  const requestUrl = new URL(req.url, 'http://localhost')
  if (requestUrl.pathname === '/fixtures/synthetic-cube.glb') {
    const model = readFileSync(modelPath)
    res.writeHead(200, { 'Content-Type': 'model/gltf-binary', 'Content-Length': model.length }); res.end(model); return
  }
  if (requestUrl.pathname === '/fixtures/synthetic-12s.mp4') {
    const stat = readFileSync(mediaPath)
    const range = req.headers.range
    res.setHeader('Accept-Ranges', 'bytes')
    res.setHeader('Content-Type', 'video/mp4')
    if (range) {
      const match = /^bytes=(\d+)-(\d*)$/.exec(range)
      if (!match) { res.writeHead(416); res.end(); return }
      const start = Number(match[1]); const end = Math.min(match[2] ? Number(match[2]) : stat.length - 1, stat.length - 1)
      if (start > end || start >= stat.length) { res.writeHead(416); res.end(); return }
      res.writeHead(206, { 'Content-Range': `bytes ${start}-${end}/${stat.length}`, 'Content-Length': end - start + 1 })
      res.end(stat.subarray(start, end + 1)); return
    }
    res.writeHead(200, { 'Content-Length': stat.length }); res.end(stat); return
  }
  let filePath = resolve(distRoot, `.${requestUrl.pathname}`)
  if (!filePath.startsWith(`${distRoot}${sep}`) || !existsSync(filePath)) filePath = resolve(distRoot, 'index.html')
  res.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream')
  createReadStream(filePath).pipe(res)
})

const artifactsDir = resolve(webRoot, 'test-results/browser-regression')
mkdirSync(artifactsDir, { recursive: true })
let browser
let context
let page
let passed = false
try {
  await new Promise((resolveListen, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolveListen) })
  const origin = `http://127.0.0.1:${server.address().port}`
  browserOrigin = origin
  browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })
  context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], reducedMotion: 'reduce' })
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true })
  page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('console', message => { if (message.type() === 'error') consoleMessages.push(message.text()) })
  page.on('pageerror', error => consoleMessages.push(`pageerror: ${error.stack || error.message}`))
  page.on('requestfailed', request => requestDiagnostics.push({ type: 'failed', method: request.method(), url: request.url(), failure: request.failure()?.errorText }))
  page.on('response', response => {
    const url = response.url()
    if (url.includes('/fixtures/synthetic-12s.mp4') || url.includes('/api/v1/public/evidence/')) requestDiagnostics.push({ type: 'response', status: response.status(), url, contentRange: response.headers()['content-range'] || null })
  })
  await page.route('**/api/v1/public/evidence/objects/**', async route => {
    const requestUrl = new URL(route.request().url())
    const params = Object.fromEntries(requestUrl.searchParams.entries())
    const response = fixture(requestUrl, params)
    apiDiagnostics.push({ url: requestUrl.toString(), status: 200, responseKeys: Object.keys(response) })
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) })
  })
  await page.route('**/api/public/clips/**', route => route.fulfill({ status: 200, contentType: 'image/png', body: readFileSync(posterPath) }))
  await page.goto(`${origin}/evidence/objects/${objectSlug}?studio=1`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Synthetic Vessel', { exact: true }).waitFor({ state: 'visible' })

  const annotationMarker = page.getByRole('button', { name: 'Vessel rim annotation', exact: true })
  await annotationMarker.waitFor({ state: 'visible', timeout: 20000 })
  await annotationMarker.click()
  await annotationMarker.waitFor({ state: 'visible' })

  const video = page.locator('video.evidence-video-element')
  await video.waitFor({ state: 'attached' })
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && node.readyState >= HTMLMediaElement.HAVE_METADATA && node.duration >= 11.9
  }, null, { timeout: 15000 })
  const verifySeek = async (expectedMs, stage) => {
    await page.waitForFunction(expected => {
      const node = document.querySelector('video.evidence-video-element')
      return node && !node.seeking && node.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && Math.abs(node.currentTime * 1000 - expected) <= 1000
    }, expectedMs, { timeout: 10000 })
    const actualMs = await video.evaluate(node => node.currentTime * 1000)
    assert.ok(Math.abs(actualMs - expectedMs) <= 1000, `${stage}: expected ${expectedMs}ms ±1000ms, received ${actualMs}ms`)
  }
  const transcriptList = page.locator('.studio-transcript-list')
  const verifyExactLineSeek = async (startMs, stage) => {
    const target = transcriptList.locator('.studio-transcript-line').filter({ hasText: `Later line at ${startMs === 3000 ? 'three seconds' : 'three and a half seconds'}` })
    await target.waitFor({ state: 'visible' })
    await target.click()
    await page.waitForFunction(expected => {
      const node = document.querySelector('video.evidence-video-element')
      return node && !node.seeking && Math.abs(node.currentTime * 1000 - expected) <= 120
    }, startMs, { timeout: 5000 })
    const actualMs = await video.evaluate(node => node.currentTime * 1000)
    assert.ok(Math.abs(actualMs - startMs) <= 180, `${stage}: expected exact line seek to ${startMs}ms, received ${actualMs}ms`)
    assert.equal(await video.evaluate(node => node.paused), false, `${stage}: transcript seek must preserve playback`)
  }
  const verifyActiveTranscriptLineVisible = async (stage) => {
    await page.waitForFunction(() => {
      const list = document.querySelector('.studio-transcript-list')
      const row = list?.querySelector('.studio-transcript-line[aria-current="true"]')
      if (!list || !row) return false
      const listRect = list.getBoundingClientRect()
      const rowRect = row.getBoundingClientRect()
      return rowRect.top >= listRect.top + 2 && rowRect.bottom <= listRect.bottom - 2
    }, null, { timeout: 5000 })
    const activeLine = await transcriptList.locator('.studio-transcript-line[aria-current="true"]').innerText()
    assert.ok(transcriptSegments.some(segment => activeLine.includes(segment.text)), `${stage}: active transcript line should match a published line`)
    const timing = await transcriptList.evaluate(list => ({ index: [...list.children].indexOf(list.querySelector('[aria-current="true"]')), timeMs: document.querySelector('video').currentTime * 1000 }))
    const segment = transcriptSegments[timing.index]
    assert.ok(segment && timing.timeMs >= segment.start_ms - 150 && timing.timeMs <= segment.end_ms + 350, `${stage}: highlighted line is out of sync with the video clock: ${JSON.stringify(timing)}`)
  }
  const verifyPlaybackStaysInFirstClip = async (stage) => {
    await page.waitForFunction(() => {
      const node = document.querySelector('video.evidence-video-element')
      return node && !node.paused && node.currentTime >= 4.2 && node.currentTime < 5.5
    }, null, { timeout: 5000 })
    const actualMs = await video.evaluate(node => node.currentTime * 1000)
    assert.ok(actualMs < 5500, `${stage}: playback jumped out of the 2–4 second clip window to ${actualMs}ms`)
  }
  const switchViewWhilePlaying = async (viewName, stage) => {
    const beforeMs = await video.evaluate(node => node.currentTime * 1000)
    await page.getByRole('radio', { name: viewName }).click()
    await page.waitForFunction(before => {
      const node = document.querySelector('video.evidence-video-element')
      return node && !node.paused && node.currentTime * 1000 > before + 120 && node.currentTime < 5.5
    }, beforeMs, { timeout: 3000 })
    await verifyActiveTranscriptLineVisible(stage)
  }
  await verifySeek(2000, 'spatial annotation linked clip A')

  // A long transcript makes each scroll-follow target move through the actual
  // list viewport. Exercise exact line selection in both Studio views, then
  // switch views while the same video moment is playing.
  await page.getByRole('radio', { name: 'Console' }).click()
  await video.evaluate(node => node.play())
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.paused && node.currentTime >= 6
  }, null, { timeout: 8000 })
  await verifyActiveTranscriptLineVisible('Console playback follow-along')

  await verifyExactLineSeek(3000, 'Console transcript line click')
  await verifyActiveTranscriptLineVisible('Console exact line highlight while playing')
  await verifyPlaybackStaysInFirstClip('Console transcript line click')
  await verifyActiveTranscriptLineVisible('Console line playback')
  await switchViewWhilePlaying('Focus', 'Focus playback follow-along')

  await verifyExactLineSeek(3500, 'Focus transcript line click')
  await verifyActiveTranscriptLineVisible('Focus exact line highlight while playing')
  await verifyPlaybackStaysInFirstClip('Focus transcript line click')
  await verifyActiveTranscriptLineVisible('Focus line playback')
  await switchViewWhilePlaying('Console', 'Console restored during playback')

  await annotationMarker.click()
  await verifySeek(2000, 'reselected spatial annotation linked clip A')
  await page.getByRole('radio', { name: 'Console' }).click()

  // A native media scrub cancels the annotation's guided clip sequence. Scrub
  // inside clip A and make sure playback continues through its old boundary
  // instead of jumping to clip B.
  await video.evaluate(node => { node.currentTime = 3.5 })
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.paused && !node.seeking && node.currentTime >= 3.45 && node.currentTime < 3.8
  }, null, { timeout: 3000 })
  await verifyPlaybackStaysInFirstClip('native scrub cancels annotation sequence')
  await verifyActiveTranscriptLineVisible('native scrub playback follows transcript')

  // A deliberate click on the Studio timeline also releases guided playback.
  await annotationMarker.click()
  await verifySeek(2000, 'annotation sequence restarted before transport scrub')
  await video.evaluate(node => node.pause())
  const seekSlider = page.getByRole('slider', { name: 'Seek video' })
  await seekSlider.scrollIntoViewIfNeeded()
  const sliderBox = await seekSlider.boundingBox()
  assert.ok(sliderBox, 'transport slider must have visible bounds')
  await page.mouse.click(sliderBox.x + sliderBox.width * (3.5 / 12), sliderBox.y + sliderBox.height / 2)
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && node.paused && !node.seeking && node.currentTime >= 3.2 && node.currentTime <= 3.8
  }, null, { timeout: 3000 })
  await video.evaluate(node => node.play())
  await verifyPlaybackStaysInFirstClip('transport scrub cancels annotation sequence')
  await verifyActiveTranscriptLineVisible('transport scrub playback follows transcript')

  // Start the annotation sequence again. It should advance from the first
  // linked clip to the second without user input, stay playing there, and pause
  // at the end of the final linked window. Switch Console/Focus during playback
  // so both view geometries keep the same guided sequence aligned.
  await annotationMarker.click()
  await verifySeek(2000, 'annotation sequence restarted after native scrub')
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.paused && node.currentTime >= 3.3 && node.currentTime < 4
  }, null, { timeout: 5000 })
  await page.getByRole('radio', { name: 'Focus' }).click()

  const linkedClipB = page.getByRole('button', { name: /Clip 2.*00:06.*00:09/ })
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    const clipB = [...document.querySelectorAll('.studio-clip-sequence button')]
      .find(button => button.textContent.includes('Clip 2'))
    return node && !node.paused && node.currentTime >= 6 && node.currentTime < 9
      && clipB?.getAttribute('aria-pressed') === 'true'
  }, null, { timeout: 5000 })
  assert.equal(await linkedClipB.getAttribute('aria-pressed'), 'true', 'guided annotation playback must automatically select linked clip B')
  await verifyActiveTranscriptLineVisible('guided playback in Focus view')

  await page.getByRole('radio', { name: 'Console' }).click()
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && !node.paused && node.currentTime >= 6.2 && node.currentTime < 9
  }, null, { timeout: 3000 })
  await verifyActiveTranscriptLineVisible('guided playback after returning to Console')
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && node.paused && node.currentTime >= 8.9 && node.currentTime < 10
  }, null, { timeout: 5000 })
  assert.equal(await linkedClipB.getAttribute('aria-pressed'), 'true', 'final linked clip remains selected when guided playback stops')
  const stoppedAtMs = await video.evaluate(node => node.currentTime * 1000)
  assert.ok(stoppedAtMs >= 8900 && stoppedAtMs < 10000, `guided sequence should stop at clip B end, received ${stoppedAtMs}ms`)

  await linkedClipB.waitFor({ state: 'visible' })
  await linkedClipB.click()
  await verifySeek(6000, 'annotation linked clip B')
  assert.equal(await linkedClipB.getAttribute('aria-pressed'), 'true', 'second linked clip must be selected in the app')
  const activeSource = await page.evaluate(() => document.querySelector('video.evidence-video-element')?.currentSrc)
  assert.ok(activeSource?.endsWith('/fixtures/synthetic-12s.mp4'), `second clip source identity mismatch: ${activeSource}`)

  await page.getByRole('button', { name: 'Copy link' }).click()
  await page.getByText('Link copied.').waitFor({ state: 'visible' })
  const shareUrl = await page.evaluate(() => navigator.clipboard.readText())
  const parsedShare = new URL(shareUrl)
  assert.equal(parsedShare.pathname, `/evidence/objects/${objectSlug}`)
  assert.equal(parsedShare.searchParams.get('clip'), secondClip.id, 'share URL must retain the selected linked segment')
  assert.equal(parsedShare.searchParams.get('annotation_context'), annotationId, 'share URL must preserve the exact selected annotation among overlapping links')
  assert.equal(parsedShare.searchParams.get('studio'), '1', 'share URL must retain the Studio surface')
  await page.goto(shareUrl, { waitUntil: 'domcontentloaded' })
  await page.getByText('Vessel rim annotation', { exact: true }).waitFor({ state: 'visible' })
  await page.waitForFunction(() => {
    const node = document.querySelector('video.evidence-video-element')
    return node && node.readyState >= HTMLMediaElement.HAVE_METADATA
  }, null, { timeout: 15000 })
  await verifySeek(6000, 'reloaded second linked clip share')
  assert.equal(await page.getByRole('button', { name: /Clip 2.*00:06.*00:09/ }).getAttribute('aria-pressed'), 'true')
  assert.equal(await page.getByRole('button', { name: 'Vessel rim annotation', exact: true }).getAttribute('aria-current'), 'true', 'clip share reload must restore its spatial annotation')

  await page.getByRole('button', { name: 'Copy citation' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Copy citation' })
  await dialog.waitFor({ state: 'visible' })
  const citationText = await dialog.locator('textarea').inputValue()
  assert.match(citationText, /Synthetic Speaker/)
  assert.match(citationText, /Synthetic Vessel/)
  assert.match(citationText, /Second linked window/)
  assert.match(citationText, /Synthetic segment two/)
  const citationLink = dialog.locator('a.evidence-citation-link')
  await citationLink.waitFor({ state: 'attached' })
  assert.equal(new URL(await citationLink.getAttribute('href')).searchParams.get('clip'), secondClip.id)
  assert.equal(new URL(await citationLink.getAttribute('href')).searchParams.get('annotation_context'), annotationId)
  assert.equal(new URL(await citationLink.getAttribute('href')).searchParams.get('studio'), '1')

  for (const response of apiDiagnostics) assert.equal(response.status, 200)
  assert.ok(apiDiagnostics.length >= 2, `expected initial and reload API requests; saw ${apiDiagnostics.length}`)
  assert.ok(requestDiagnostics.some(record => record.url.includes('/fixtures/synthetic-12s.mp4') && record.status === 206), 'real media server must service a byte-range response')
  const publicBodies = apiDiagnostics.map(({ url }) => fixture(new URL(url), Object.fromEntries(new URL(url).searchParams.entries())))
  for (const body of publicBodies) {
    const serialized = JSON.stringify(body)
    for (const field of privateFields) assert.ok(!serialized.includes(`"${field}"`), `public response leaked private field ${field}`)
  }
  assert.deepEqual(consoleMessages, [], `browser console errors: ${consoleMessages.join('\n')}`)
  passed = true
  console.log(JSON.stringify({ result: 'PASS', candidate: process.env.GITHUB_SHA || 'local-working-tree', browser: 'Chromium with WebGL via SwiftShader', mediaSha256: actualSha256, modelSha256: actualModelSha256, posterSha256: actualPosterSha256, mediaBytes: readFileSync(mediaPath).length, apiMode: 'synthetic public contract route; real public API projection/privacy test required in API job', requests: apiDiagnostics.length, seekToleranceMs: 1000 }, null, 2))
} catch (error) {
  failures.push(error.stack || String(error))
  if (page) {
    await page.screenshot({ path: resolve(artifactsDir, 'failure.png'), fullPage: true }).catch(() => {})
    await import('node:fs/promises').then(fs => fs.writeFile(resolve(artifactsDir, 'diagnostics.json'), JSON.stringify({ failures, consoleMessages, requestDiagnostics, apiDiagnostics, url: page.url() }, null, 2)))
  }
  console.error(JSON.stringify({ result: 'FAIL', failures, consoleMessages, requestDiagnostics, apiDiagnostics, artifactsDir }, null, 2))
  process.exitCode = 1
} finally {
  if (context) await context.tracing.stop({ path: resolve(artifactsDir, passed ? 'trace.zip' : 'failure-trace.zip') }).catch(() => {})
  if (browser) await browser.close()
  await new Promise(resolveClose => server.close(resolveClose))
}
