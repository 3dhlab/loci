// Synthetic responses test console layout, retained themes and observed placement failures.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { existsSync, mkdirSync, readFileSync, writeFileSync, statSync } from 'node:fs'
import { extname, resolve, sep } from 'node:path'
import { chromium } from 'playwright'

const root = resolve(import.meta.dirname, '../..')
const dist = resolve(root, 'dist-ci')
const mediaRoot = resolve(root, 'tests/fixtures')
const artifacts = resolve(root, 'test-results/browser-regression/console-surface')
mkdirSync(artifacts, { recursive: true })
const ids = { project: '10000000-0000-4000-8000-000000000001', object: '10000000-0000-4000-8000-000000000002', video: '10000000-0000-4000-8000-000000000005', model: '10000000-0000-4000-8000-000000000003', transcript: '10000000-0000-4000-8000-000000000006' }
const project = { id: ids.project, name: 'Synthetic review collection', slug: 'synthetic-review', is_published: true }
const object = { id: ids.object, project_id: ids.project, title: 'Demo cube', description: 'Generated review fixture', website_object_id: 'demo-cube', is_published: true, is_embed_ready: true }
const video = { id: ids.video, project_id: ids.project, object_id: ids.object, title: 'Three cube views', stable_video_id: 'demo-colors', status: 'READY', duration_ms: 12000, is_published: true, updated_at: '2026-10-04T00:00:00Z' }
const raw = 'WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nSection 1: Front face. A square marks the front face of the cube.\n'
const transcript = { id: ids.transcript, video_id: ids.video, title: 'Cube view demonstration transcript', format: 'VTT', language: 'en', raw_text: raw, is_published: true }
const segments = [0, 1, 2].map(i => ({ id: `synthetic-segment-${i}`, position: i, start_ms: i * 4000, end_ms: (i + 1) * 4000, text: ['Section 1: Front face. A square marks the front face of the cube.', 'Section 2: Top edge. A triangle marks the top edge of the cube.', 'Section 3: Compare view. A circle marks the side face of the cube.'][i] }))
const model = { id: ids.model, object_id: ids.object, revision: 1, original_filename: 'cube.glb', file_size_bytes: 896, is_published: true, model_transform_json: null, default_camera_json: null }
const annotations = []
const posts = []
const unknown = new Set()
let origin = ''
const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.glb': 'model/gltf-binary' }
function sendFile(req, res, path) {
  const bytes = readFileSync(path)
  const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '')
  res.setHeader('Content-Type', types[extname(path)] || 'application/octet-stream')
  res.setHeader('Accept-Ranges', 'bytes')
  if (match) {
    const start = +match[1], end = Math.min(match[2] ? +match[2] : bytes.length - 1, bytes.length - 1)
    if (start > end || start >= bytes.length) { res.writeHead(416); res.end(); return }
    res.writeHead(206, { 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Content-Length': end - start + 1 })
    res.end(bytes.subarray(start, end + 1)); return
  }
  res.writeHead(200, { 'Content-Length': bytes.length }); res.end(bytes)
}
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost'), path = url.pathname
  if (path.endsWith('/stream') || path === '/fixtures/colors.mp4') { sendFile(req, res, resolve(mediaRoot, 'synthetic-12s.mp4')); return }
  if (path.endsWith('/model/file') || path === '/fixtures/cube.glb') { sendFile(req, res, resolve(mediaRoot, 'synthetic-cube.glb')); return }
  if (path.startsWith('/api/')) {
    let payload = []
    if (req.method === 'POST') {
      let body = ''; for await (const chunk of req) body += chunk
      const value = JSON.parse(body || '{}'); posts.push({ path, value })
      if (path.endsWith('/annotations')) { payload = { ...value, id: 'synthetic-created', review_status: 'ACTIVE', is_published: false }; annotations.push(payload) }
      else payload = { detail: 'Synthetic review only' }
    } else if (path === '/api/v1/projects') payload = [project]
    else if (path === '/api/v1/objects') payload = [object]
    else if (path === '/api/v1/videos') payload = [video]
    else if (path === '/api/v1/transcripts') payload = [transcript]
    else if (path.startsWith('/api/v1/transcripts/videos/')) payload = { transcript, segments }
    else if (path.endsWith('/model/annotations')) payload = annotations
    else if (path.endsWith('/model')) payload = model
    else if (path.startsWith('/api/v1/clips')) payload = { clips: [] }
    else if (path === '/api/v1/ops/live-database/sync/status') payload = { state: 'IDLE', enabled: false }
    else if (path === '/api/v1/videos/media-files') payload = []
    else { unknown.add(path); payload = { state: 'IDLE', enabled: false, items: [], results: [] } }
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(payload)); return
  }
  let file = resolve(dist, `.${path}`)
  if (!file.startsWith(`${dist}${sep}`) || !existsSync(file) || !statSync(file).isFile()) file = resolve(dist, 'index.html')
  sendFile(req, res, file)
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
origin = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })
const report = { api: 'Synthetic review responses; no backend, real account, or database', origin, login: [], authoring: [], errors: [] }
async function measures(page) {
  return page.evaluate(() => {
    const logo = document.querySelector('.brand-lockup')
    const boxes = [...document.querySelectorAll('.model-layout > *')].map(el => ({ class: el.className, rect: { x: el.getBoundingClientRect().x, y: el.getBoundingClientRect().y, width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height } }))
    return { width: innerWidth, documentWidth: document.documentElement.scrollWidth, palette: document.querySelector('main')?.dataset.studioTheme, logoFilter: logo ? getComputedStyle(logo).filter : null, logoLoaded: logo?.complete && logo.naturalWidth > 0, authoringClass: document.documentElement.classList.contains('authoring-document-surface'), boxes }
  })
}
async function checkContrast(page, selectors = ['.login-card label', '.login-card .muted', '.login-card button[type=submit]']) {
  const results = await page.evaluate(selectors => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1
    const ctx = canvas.getContext('2d')
    const rgb = color => {
      ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1)
      return [...ctx.getImageData(0, 0, 1, 1).data]
    }
    const luminance = color => rgb(color).slice(0, 3).map(v => v / 255)
      .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
      .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0)
    return selectors.flatMap(selector => [...document.querySelectorAll(selector)].map(el => {
      let node = el
      while (node && rgb(getComputedStyle(node).backgroundColor)[3] === 0) node = node.parentElement
      const background = getComputedStyle(node).backgroundColor
      const text = getComputedStyle(el).color
      const [high, low] = [luminance(text), luminance(background)].sort((a, b) => b - a)
      return { selector, text, background, ratio: (high + .05) / (low + .05) }
    }))
  }, selectors)
  assert.ok(results.length > 0)
  for (const result of results) assert.ok(result.ratio >= 4.5, `${result.selector}: ${result.ratio.toFixed(2)}:1`)
}
try {
  for (const theme of ['light', 'dark', 'muted-light']) {
    const context = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 1000 } })
    await context.addInitScript(theme => localStorage.setItem('loci.studio.theme', theme), theme)
    const page = await context.newPage(); page.setDefaultTimeout(15000)
    page.on('pageerror', error => report.errors.push(error.message))
    await page.goto(`${origin}/console`); await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor()
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.screenshot({ path: resolve(artifacts, `login-${theme}-${width}.png`) })
      const metrics = await measures(page)
      assert.equal(metrics.documentWidth, width, 'Login fits the viewport')
      assert.equal(metrics.logoFilter, 'none', 'The opaque logo preserves its lettering')
      assert.ok(metrics.logoLoaded)
      assert.equal(metrics.palette, { light: 'cobalt', dark: 'darkroom', 'muted-light': 'muted-light' }[theme])
      await checkContrast(page)
      report.login.push({ theme, ...metrics })
    }
    await page.goto(`${origin}/preview`); await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor()
    report.login.push({ theme, path: '/preview', ...(await measures(page)) })
    await page.screenshot({ path: resolve(artifacts, `preview-${theme}-390.png`) })
    await context.close()
  }
  const context = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 1000 } })
  await context.addInitScript(() => { localStorage.setItem('semantic.console.token', 'synthetic-review-token'); localStorage.setItem('loci.studio.theme', 'dark') })
  const page = await context.newPage(); page.setDefaultTimeout(15000)
  page.on('pageerror', error => report.errors.push(error.message))
  await page.goto(`${origin}/console`)
  const draft = page.getByPlaceholder('Transcript raw text')
  await page.waitForFunction(() => document.querySelector('textarea[placeholder="Transcript raw text"]')?.value.startsWith('WEBVTT'))
  const edit = `${raw}\nSynthetic unsaved edit`
  await draft.fill(edit)
  await page.getByRole('button', { name: '3D Model', exact: true }).click()
  await page.getByRole('heading', { name: 'Annotation Editor', exact: true }).waitFor()
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    const metrics = await measures(page)
    assert.equal(metrics.documentWidth, width, 'The authoring workspace fits the viewport')
    report.authoring.push(metrics)
    await page.screenshot({ path: resolve(artifacts, `workspace-dark-${width}.png`), fullPage: true })
  }
  await page.getByRole('button', { name: 'Analysis', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('textarea[placeholder="Transcript raw text"]')?.value.includes('Synthetic unsaved edit'))
  assert.equal(await draft.inputValue(), edit)
  await page.getByRole('button', { name: 'Undo', exact: true }).click()
  assert.equal(await draft.inputValue(), raw)
  report.draftRoundtripAndUndo = 'PASS with synthetic API responses'
  await page.getByRole('button', { name: '3D Model', exact: true }).click()
  const editor = page.locator('.model-sidebar .card').filter({ has: page.getByRole('heading', { name: 'Annotation Editor', exact: true }) })
  await editor.getByPlaceholder('Annotation title', { exact: true }).fill('Untouched point review probe')
  await editor.getByPlaceholder('Start ms', { exact: true }).fill('0')
  await editor.getByPlaceholder('End ms', { exact: true }).fill('4000')
  await editor.getByRole('button', { name: 'Create annotation', exact: true }).click()
  await page.getByText('Click the model to capture a valid annotation point before saving.', { exact: true }).waitFor()
  assert.equal(posts.filter(p => p.path.endsWith('/annotations')).length, 0, 'Blank placement never reaches the API')
  report.blankPlacementRejected = true
  await editor.getByRole('button', { name: 'Capture point on model', exact: true }).click()
  const canvas = page.locator('.model-canvas-panel canvas').first()
  await canvas.waitFor()
  await page.waitForFunction(() => !document.querySelector('.model-canvas-loading-overlay.active'))
  const box = await canvas.boundingBox()
  assert.ok(box)
  await canvas.click({ position: { x: box.width * .5, y: box.height * .54 } })
  await editor.getByText('Captured', { exact: true }).waitFor()
  await editor.getByRole('button', { name: 'Create annotation', exact: true }).click()
  await page.getByText('3D annotation created.', { exact: true }).waitFor()
  const creation = posts.find(p => p.path.endsWith('/annotations'))
  assert.ok([creation.value.point_x, creation.value.point_y, creation.value.point_z].every(Number.isFinite))
  const selected = page.locator('.model-annotation-item.selected')
  await selected.waitFor()
  await checkContrast(page, ['.model-annotation-item.selected strong', '.model-annotation-item.selected span', '.model-annotation-item.selected button'])
  report.capturedSurfacePointAccepted = true
  for (const theme of ['light', 'muted-light']) {
    await page.evaluate(theme => localStorage.setItem('loci.studio.theme', theme), theme)
    await page.reload()
    await page.getByRole('button', { name: '3D Model', exact: true }).click()
    const row = page.locator('.model-annotation-item').filter({ hasText: 'Untouched point review probe' })
    await row.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.locator('.model-annotation-item.selected').waitFor()
    await checkContrast(page, ['.model-annotation-item.selected strong', '.model-annotation-item.selected span', '.model-annotation-item.selected button'])
    await page.screenshot({ path: resolve(artifacts, `selected-${theme}.png`), fullPage: true })
  }
  assert.equal(unknown.size, 0, 'The console only requests the expected synthetic endpoints')
  report.unknownApiPaths = [...unknown]
  assert.deepEqual(report.errors, [])
  report.result = 'PASS'
  await context.close()
} catch (error) {
  report.failure = error.stack; process.exitCode = 1
} finally {
  await browser.close(); await new Promise(r => server.close(r))
  writeFileSync(resolve(artifacts, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
}
