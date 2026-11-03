// Actual built console with a loopback-only synthetic API. This verifies the
// browser's request/status ownership; it does not prove a durable server effect.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { existsSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import { extname, resolve, sep } from 'node:path'
import { chromium } from 'playwright'

const root = resolve(import.meta.dirname, '../..')
const dist = realpathSync(resolve(root, 'dist-ci'))
const fixtures = resolve(root, 'tests/fixtures')
const artifacts = resolve(root, 'test-results/browser-regression/publication-status')
mkdirSync(artifacts, { recursive: true })
const ids = { project: '10000000-0000-4000-8000-000000000001', object: '10000000-0000-4000-8000-000000000002', model: '10000000-0000-4000-8000-000000000003', video: '10000000-0000-4000-8000-000000000005', transcript: '10000000-0000-4000-8000-000000000006' }
const project = { id: ids.project, name: 'Synthetic publication collection', is_published: true }
const object = { id: ids.object, project_id: ids.project, name: 'Synthetic publication cube', title: 'Synthetic publication cube', website_object_id: 'synthetic-publication-cube', is_published: true }
const video = { id: ids.video, project_id: ids.project, object_id: ids.object, title: 'Generated cube views', status: 'READY', duration_ms: 12000, is_published: true }
const transcript = { id: ids.transcript, video_id: ids.video, title: 'Generated transcript', format: 'VTT', language: 'en', raw_text: 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nGenerated publication fixture.\n', is_published: true }
const model = { id: ids.model, object_id: ids.object, revision: 1, original_filename: 'synthetic-cube.glb', is_published: true }
const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.glb': 'model/gltf-binary' }
const report = { scope: 'built React console with synthetic loopback API; not real publication, database or durability proof', cases: [], redirectedDevelopmentApi: [], expectedLostAcknowledgementFailures: [], blockedExternal: [], unknownApi: [], pageErrors: [] }
let active
let browser
let origin

function deferred() {
  let resolvePromise
  const promise = new Promise((resolve) => { resolvePromise = resolve })
  return { promise, resolve: resolvePromise }
}

async function waitFor(promise, label) {
  let timer
  try {
    return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out: ${label}`)), 15000) })])
  } finally {
    clearTimeout(timer)
  }
}

function status(state, operationId = null) {
  return { operation_id: operationId, state, stage_key: 'synthetic-fixture', stage_label: `${operationId || 'Service'} ${state}`, detail: `Synthetic ${operationId || 'service'} ${state}`, progress_percent: state === 'SUCCEEDED' ? 100 : 0, project_count: 0, object_count: 0, video_count: 0, transcript_count: 0, annotation_count: 0, media_file_count: 0 }
}

function sendFile(req, res, file) {
  const bytes = readFileSync(file)
  const range = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '')
  res.setHeader('Content-Type', types[extname(file)] || 'application/octet-stream')
  res.setHeader('Accept-Ranges', 'bytes')
  if (range) {
    const start = Number(range[1])
    const end = Math.min(range[2] ? Number(range[2]) : bytes.length - 1, bytes.length - 1)
    if (start > end || start >= bytes.length) { res.writeHead(416); res.end(); return }
    res.writeHead(206, { 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Content-Length': end - start + 1 })
    res.end(bytes.subarray(start, end + 1))
    return
  }
  res.writeHead(200, { 'Content-Length': bytes.length })
  res.end(bytes)
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost')
    const path = url.pathname
    if (path.endsWith('/model/file')) { sendFile(req, res, resolve(fixtures, 'synthetic-cube.glb')); return }
    if (path.endsWith('/stream')) { sendFile(req, res, resolve(fixtures, 'synthetic-12s.mp4')); return }
    if (path.endsWith('/poster')) { sendFile(req, res, resolve(fixtures, 'synthetic-poster.png')); return }
    if (path.startsWith('/api/')) {
      let payload
      if (path === '/api/v1/ops/live-database/sync/start') {
        assert.equal(req.method, 'POST')
        const key = req.headers['idempotency-key']
        assert.match(key || '', /^[\x21-\x7e]{1,256}$/)
        active.posts.push(key)
        payload = await active.start(req, res)
        if (res.destroyed) return
      } else if (path === '/api/v1/ops/live-database/sync/capabilities') {
        if (!active.durable) { res.writeHead(404, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ detail: 'Legacy fixture has no capability endpoint' })); return }
        payload = { protocol_version: 1, destination: 'configured-public', publication_capabilities: { durable_requests: !active.capabilitiesUnavailable, request_lookup: !active.capabilitiesUnavailable, reconciliation: !active.capabilitiesUnavailable, cancellation: !active.capabilitiesUnavailable } }
      }
      else if (path === '/api/v1/auth/me') payload = { id: '10000000-0000-4000-8000-000000000099', is_active: true }
      else if (path === '/api/v1/ops/live-database/sync/status') {
        const idempotencyKey = req.headers['idempotency-key']
        if (idempotencyKey) active.keyReads.push(idempotencyKey)
        payload = await active.read({ idempotencyKey })
      }
      else if (/^\/api\/v1\/ops\/live-database\/sync\/[^/]+\/reconcile$/.test(path)) {
        assert.equal(req.method, 'POST')
        active.reconciles += 1
        payload = await active.reconcile()
      }
      else if (/^\/api\/v1\/ops\/live-database\/sync\/[^/]+\/cancel$/.test(path)) {
        assert.equal(req.method, 'POST')
        active.cancels += 1
        payload = await active.cancel()
      }
      else if (path.startsWith('/api/v1/ops/live-database/sync/operations/')) {
        const operationId = decodeURIComponent(path.slice('/api/v1/ops/live-database/sync/operations/'.length))
        active.operationReads.push(operationId)
        payload = await active.read({ operationId })
      }
      else if (path === '/api/v1/projects') payload = [project]
      else if (path === '/api/v1/objects') payload = [object]
      else if (path === '/api/v1/videos') payload = [video]
      else if (path === '/api/v1/transcripts') payload = [transcript]
      else if (path.startsWith('/api/v1/transcripts/videos/')) payload = { transcript, segments: [] }
      else if (path.endsWith('/model/annotations')) payload = []
      else if (path.endsWith('/model')) payload = model
      else if (path.startsWith('/api/v1/clips')) payload = { clips: [] }
      else if (path === '/api/v1/videos/media-files') payload = []
      else if (path === '/api/v1/search/tuning/summary') payload = { pending_count: 0, reviewed_count: 0 }
      else {
        report.unknownApi.push(path)
        res.writeHead(404, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'No synthetic fixture for this request' }))
        return
      }
      if (!res.destroyed) {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify(payload))
      }
      return
    }
    let file = resolve(dist, `.${path}`)
    if (['/', '/console'].includes(path)) file = resolve(dist, 'index.html')
    if (!file.startsWith(`${dist}${sep}`) || !existsSync(file) || !statSync(file).isFile() || !realpathSync(file).startsWith(`${dist}${sep}`)) {
      res.writeHead(404); res.end(); return
    }
    sendFile(req, res, file)
  } catch (error) {
    if (!res.destroyed) { res.writeHead(500); res.end('Synthetic fixture failure') }
    report.fixtureError = error.message
  }
})

async function openConsole(fixture) {
  active = { posts: [], operationReads: [], keyReads: [], reconciles: 0, cancels: 0, ...fixture }
  const fixtureState = active
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', serviceWorkers: 'block' })
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin === origin) { await route.continue(); return }
    // The source build defaults to hostname:8000. Keep those requests on the
    // controlled fixture instead of contacting an ambient development service.
    if (url.origin === 'http://127.0.0.1:8000' && url.pathname.startsWith('/api/')) {
      report.redirectedDevelopmentApi.push({ method: route.request().method(), path: url.pathname })
      let response
      try {
        response = await route.fetch({ url: `${origin}${url.pathname}${url.search}`, maxRetries: 0, maxRedirects: 0 })
      } catch (error) {
        if (!fixtureState.expectLostAcknowledgement || fixtureState.posts.length !== 1 || route.request().method() !== 'POST' || url.pathname !== '/api/v1/ops/live-database/sync/start') throw error
        report.expectedLostAcknowledgementFailures.push({ method: 'POST', path: url.pathname })
        await route.abort('connectionfailed')
        return
      }
      await route.fulfill({ response, headers: { ...response.headers(), 'access-control-allow-origin': origin } })
      return
    }
    report.blockedExternal.push({ method: route.request().method(), origin: url.origin })
    await route.abort('blockedbyclient')
  })
  await context.addInitScript(() => localStorage.setItem('semantic.console.token', 'synthetic-publication-test-token'))
  const page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', (error) => report.pageErrors.push(error.message))
  await page.goto(`${origin}/console`)
  await page.getByRole('button', { name: '3D Model', exact: true }).click()
  await page.getByRole('heading', { name: 'Annotation Editor', exact: true }).waitFor()
  await page.locator('.publication-sync-button').waitFor()
  return { page, context }
}

async function start(page) {
  await page.locator('.publication-sync-button').click()
  await page.getByRole('dialog', { name: 'Sync Live Database?' }).getByRole('button', { name: 'Sync Live Database', exact: true }).click()
}

async function deliveredStatus(page, operationId, state) {
  const response = await page.waitForResponse(async (response) => {
    if (response.request().method() !== 'GET' || new URL(response.url()).pathname !== '/api/v1/ops/live-database/sync/status') return false
    const payload = await response.json()
    return payload.operation_id === operationId && payload.state === state
  })
  assert.equal(await response.finished(), null)
  await page.evaluate(() => new Promise((resolveFrame) => requestAnimationFrame(() => requestAnimationFrame(resolveFrame))))
}

try {
  await new Promise((resolveListen, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  assert.equal(server.address().address, '127.0.0.1')
  origin = `http://127.0.0.1:${server.address().port}`
  browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'] })

  const older = deferred()
  const olderSeen = deferred()
  let aReads = 0
  let starts = 0
  let live = 'IDLE'
  const a = await openConsole({
    async start() { starts += 1; live = starts === 1 ? 'A' : 'B'; return status('RUNNING', live) },
    async read() {
      if (live === 'A') {
        aReads += 1
        if (aReads === 1) { olderSeen.resolve(); return older.promise }
        live = 'A-complete'
        return status('SUCCEEDED', 'A')
      }
      if (live === 'B') return status('RUNNING', 'B')
      return status('IDLE')
    }
  })
  try {
    await start(a.page)
    await waitFor(olderSeen.promise, 'first A poll')
    await a.page.locator('.publication-sync-status').getByText('A SUCCEEDED', { exact: true }).waitFor()
    await start(a.page)
    await a.page.locator('.publication-sync-status').getByText('B RUNNING', { exact: true }).waitFor()
    const lateADelivered = deliveredStatus(a.page, 'A', 'SUCCEEDED')
    older.resolve(status('SUCCEEDED', 'A'))
    await lateADelivered
    assert.equal(await a.page.locator('.publication-sync-button').isDisabled(), true)
    assert.ok((await a.page.locator('.publication-sync-status').textContent()).includes('B RUNNING'))
    assert.ok(!(await a.page.locator('.publication-sync-status').textContent()).includes('A SUCCEEDED'))
    assert.equal(active.posts.length, 2)
    assert.notEqual(active.posts[0], active.posts[1])
    await a.page.screenshot({ path: resolve(artifacts, 'late-a-keeps-b-running.png') })
    report.cases.push({ name: 'late A completion cannot replace current B or stop its polling', passed: true })
  } finally { older.resolve(status('SUCCEEDED', 'A')); await a.context.close() }

  const initial = deferred()
  const initialSeen = deferred()
  const post = deferred()
  const postSeen = deferred()
  let firstRead = true
  const b = await openConsole({
    async start() { postSeen.resolve(); return post.promise },
    async read() {
      if (firstRead) { firstRead = false; initialSeen.resolve(); return initial.promise }
      return status('RUNNING', 'C')
    }
  })
  try {
    await waitFor(initialSeen.promise, 'initial status request')
    await start(b.page)
    await waitFor(postSeen.promise, 'pending POST')
    const initialDelivered = deliveredStatus(b.page, null, 'IDLE')
    initial.resolve(status('IDLE'))
    await initialDelivered
    assert.equal(await b.page.locator('.publication-sync-button').isDisabled(), true)
    post.resolve(status('RUNNING', 'C'))
    await b.page.locator('.publication-sync-status').getByText('C RUNNING', { exact: true }).waitFor()
    assert.equal(await b.page.locator('.publication-sync-button').isDisabled(), true)
    report.cases.push({ name: 'late GET does not release pending POST busy ownership', passed: true })
  } finally { initial.resolve(status('IDLE')); post.resolve(status('RUNNING', 'C')); await b.context.close() }

  const c = await openConsole({
    expectLostAcknowledgement: true,
    async start(_req, res) {
      if (active.posts.length === 1) { res.destroy(); return null }
      return status('RUNNING', 'D')
    },
    async read() { return status('IDLE') }
  })
  try {
    await start(c.page)
    await c.page.getByRole('button', { name: 'Retry Live Database request', exact: true }).waitFor()
    await c.page.waitForTimeout(200)
    assert.equal(active.posts.length, 1, 'POST is not automatically retried')
    await start(c.page)
    await c.page.locator('.publication-sync-status').getByText('D RUNNING', { exact: true }).waitFor()
    assert.equal(active.posts.length, 2)
    assert.equal(active.posts[0], active.posts[1])
    report.cases.push({ name: 'explicit retry after lost acknowledgement retains request key', passed: true, serverScope: 'synthetic fixture only; legacy backend has no durable replay guarantee' })
  } finally { await c.context.close() }

  const recovered = await openConsole({
    async start() { return { ...status('RUNNING', 'E'), destination: 'synthetic-local' } },
    async read({ operationId } = {}) {
      return operationId ? { ...status('RUNNING', operationId), destination: 'synthetic-local' } : status('IDLE')
    }
  })
  try {
    await start(recovered.page)
    await recovered.page.locator('.publication-sync-status').getByText('E RUNNING', { exact: true }).waitFor()
    assert.equal(active.posts.length, 1)
    await recovered.page.reload()
    await recovered.page.getByRole('button', { name: '3D Model', exact: true }).click()
    await recovered.page.locator('.publication-sync-status').getByText('E RUNNING', { exact: true }).waitFor()
    assert.ok(active.operationReads.includes('E'), 'Reload recovers the exact operation')
    const operationReadsAfterReload = active.operationReads.length
    const otherTab = await recovered.context.newPage()
    await otherTab.goto(`${origin}/console`)
    await otherTab.getByRole('button', { name: '3D Model', exact: true }).click()
    await otherTab.locator('.publication-sync-status').getByText('E RUNNING', { exact: true }).waitFor()
    assert.ok(active.operationReads.length > operationReadsAfterReload, 'New tab reads the exact operation')
    assert.equal(active.posts.length, 1, 'Recovery never submits another publication')
    assert.equal(await otherTab.locator('.publication-sync-button').isDisabled(), true)
    report.cases.push({ name: 'reload and new tab recover acknowledged durable operation through exact reads without POST', passed: true,
      serverScope: 'synthetic advertised durable API; real legacy transport is deliberately not persisted' })
  } finally { await recovered.context.close() }

  const uncertain = await openConsole({
    async start() { return status('NEEDS_RECONCILIATION', 'F') },
    async read() { return status('IDLE') }
  })
  try {
    await start(uncertain.page)
    await uncertain.page.getByRole('button', { name: 'Publication requires reconciliation', exact: true }).waitFor()
    assert.equal(await uncertain.page.locator('.publication-sync-button').isDisabled(), true)
    await uncertain.page.getByRole('status').filter({ hasText: 'The remote outcome has not been confirmed.' }).waitFor()
    assert.equal(active.posts.length, 1)
    report.cases.push({ name: 'uncertain remote effect blocks another publication and displays reconciliation requirement', passed: true })
  } finally { await uncertain.context.close() }

  const durableCapabilities = { durable_requests: true, request_lookup: true, reconciliation: true, cancellation: true }
  function realStatus(state, operationId) {
    return { ...status(state, operationId), destination: 'configured-public', publication_capabilities: durableCapabilities,
      generation_id: `generation-${operationId}`, selection_digest: 'a'.repeat(64) }
  }
  const allowKeyLookup = deferred()
  const lost = await openConsole({
    durable: true, expectLostAcknowledgement: true,
    async start(_req, res) { res.destroy(); return null },
    async read({ idempotencyKey } = {}) {
      if (!idempotencyKey) return status('IDLE')
      await allowKeyLookup.promise
      assert.equal(idempotencyKey, active.posts[0])
      return realStatus('RUNNING', 'G')
    }
  })
  try {
    await start(lost.page)
    await lost.page.waitForFunction(() => Object.keys(localStorage).some((key) => {
      if (!key.startsWith('loci.publication.intent.v1:')) return false
      const value = JSON.parse(localStorage.getItem(key))
      return value.version === 2 && value.operationId === null
    }))
    await lost.page.locator('.publication-sync-status').getByText('The acknowledgement was lost. Checking the saved request without resubmitting publication.', { exact: true }).waitFor()
    assert.equal(active.posts.length, 1)
    await lost.page.reload()
    await lost.page.getByRole('button', { name: '3D Model', exact: true }).click()
    allowKeyLookup.resolve()
    await lost.page.locator('.publication-sync-status').getByText('G RUNNING', { exact: true }).waitFor()
    assert.ok(active.keyReads.length >= 1)
    assert.ok(active.keyReads.every((key) => key === active.posts[0]))
    assert.equal(active.posts.length, 1, 'Lost acknowledgement recovery never repeats publication')
    assert.equal(await lost.page.locator('.publication-sync-button').isDisabled(), true)
    report.cases.push({ name: 'pending key survives lost acknowledgement and reload; authorized key lookup recovers without another POST', passed: true,
      serverScope: 'synthetic durable API contract; real worker/database acceptance remains separate' })
  } finally { allowKeyLookup.resolve(); await lost.context.close() }

  const reconciled = await openConsole({
    durable: true,
    async start() { return realStatus('NEEDS_RECONCILIATION', 'H') },
    async read() { return status('IDLE') },
    async reconcile() {
      const payload = realStatus('SUCCEEDED', 'H')
      payload.receipt = { operation_id: 'H', generation_id: payload.generation_id, selection_digest: payload.selection_digest,
        http_readback: { status: 'verified', resource_count: 4, verified_at: '2026-11-03T20:00:00Z' } }
      return payload
    }
  })
  try {
    await start(reconciled.page)
    await reconciled.page.getByRole('button', { name: 'Publication requires reconciliation', exact: true }).waitFor()
    assert.equal(active.reconciles, 0)
    await reconciled.page.getByRole('button', { name: 'Recheck remote publication', exact: true }).click()
    await reconciled.page.getByText('Verified live generation', { exact: true }).waitFor()
    await reconciled.page.getByText('4 public resources verified by destination readback.', { exact: true }).waitFor()
    assert.equal(active.reconciles, 1)
    assert.equal(active.posts.length, 1, 'Reconciliation uses only the distinct target-readback endpoint')
    await reconciled.page.getByText('Source preview visibility. The live destination result is shown in the sync status.', { exact: true }).waitFor()
    report.cases.push({ name: 'explicit readback reconciliation shows matching verified generation while distinguishing preview visibility', passed: true,
      serverScope: 'fixture receipt; actual destination integrity requires backend tests' })
  } finally { await reconciled.context.close() }

  const unavailable = await openConsole({
    durable: true, capabilitiesUnavailable: true,
    async start() { return realStatus('QUEUED', 'I') },
    async read() { return status('IDLE') }
  })
  try {
    const card = unavailable.page.locator('.model-publication-card')
    await card.getByText('Durable publication is unavailable on this server. Saved request metadata is retained. Retry access verification before publishing.', { exact: true }).waitFor()
    assert.equal(await unavailable.page.locator('.publication-sync-button').isDisabled(), true)
    assert.equal(active.posts.length, 0, 'Unavailable capabilities never cause a legacy fallback POST')
    active.capabilitiesUnavailable = false
    await card.getByRole('button', { name: 'Retry publication access verification', exact: true }).click()
    await unavailable.page.getByRole('button', { name: 'Sync Live Database', exact: true }).waitFor()
    assert.equal(active.posts.length, 0, 'Retrying access is read-only')
    await start(unavailable.page)
    await unavailable.page.locator('.publication-sync-status').getByText('I QUEUED', { exact: true }).waitFor()
    assert.equal(active.posts.length, 1, 'Publication needs a separate explicit user action after access recovers')
    report.cases.push({ name: 'unavailable durable capability blocks publication until read-only access verification recovers', passed: true })
  } finally { await unavailable.context.close() }

  const closed = await openConsole({
    durable: true,
    async start() { return realStatus('QUEUED', 'J') },
    async read() { return status('IDLE') },
    async cancel() { return { ...realStatus('CANCELLED', 'J'), detail: 'The queued request was closed before publication.' } }
  })
  try {
    await start(closed.page)
    await closed.page.locator('.publication-sync-status').getByText('J QUEUED', { exact: true }).waitFor()
    await closed.page.getByRole('button', { name: 'Close publication request', exact: true }).click()
    const dialog = closed.page.getByRole('dialog', { name: 'Close publication request?', exact: true })
    await dialog.getByText('Close this request and prevent a late publication attempt using the destination cancellation fence. This cannot undo an already committed publication. If publication already completed, the verified result will be retained.', { exact: true }).waitFor()
    assert.equal(active.cancels, 0, 'Opening confirmation cannot cancel the request')
    await dialog.getByRole('button', { name: 'Close request', exact: true }).click()
    await closed.page.locator('.publication-sync-status').getByText('J CANCELLED', { exact: true }).waitFor()
    assert.equal(active.cancels, 1)
    assert.equal(active.posts.length, 1)
    assert.equal(await closed.page.getByText('Verified live generation', { exact: true }).count(), 0)
    report.cases.push({ name: 'explicit confirmed queued cancellation closes the request without a publication or positive receipt claim', passed: true })
  } finally { await closed.context.close() }

  const superseded = await openConsole({
    durable: true,
    async start() { return realStatus('NEEDS_RECONCILIATION', 'K') },
    async read() { return status('IDLE') },
    async cancel() { return { ...realStatus('SUPERSEDED', 'K'), detail: 'This generation was previously published; the destination now has a different head.' } }
  })
  try {
    await start(superseded.page)
    await superseded.page.getByRole('button', { name: 'Publication requires reconciliation', exact: true }).waitFor()
    await superseded.page.getByRole('button', { name: 'Close publication request', exact: true }).click()
    await superseded.page.getByRole('dialog', { name: 'Close publication request?', exact: true }).getByRole('button', { name: 'Close request', exact: true }).click()
    await superseded.page.locator('.publication-sync-status').getByText('K SUPERSEDED', { exact: true }).waitFor()
    await superseded.page.locator('.publication-sync-status').getByText('Superseded (previously published) · 0%', { exact: true }).waitFor()
    assert.equal(active.cancels, 1)
    assert.equal(active.posts.length, 1)
    assert.equal(await superseded.page.getByText('Verified live generation', { exact: true }).count(), 0)
    assert.equal(await superseded.page.locator('.publication-sync-button').isDisabled(), false)
    report.cases.push({ name: 'superseded uncertain request becomes terminal while preserving the distinction between prior publication and current head', passed: true })
  } finally { await superseded.context.close() }

  assert.deepEqual(report.blockedExternal, [])
  const usesDevelopmentProxy = report.redirectedDevelopmentApi.some((request) => request.method === 'POST')
  assert.deepEqual(report.expectedLostAcknowledgementFailures, usesDevelopmentProxy ? Array.from({ length: 2 }, () => ({ method: 'POST', path: '/api/v1/ops/live-database/sync/start' })) : [])
  assert.deepEqual(report.unknownApi, [])
  assert.deepEqual(report.pageErrors, [])
  assert.equal(report.fixtureError, undefined)
  report.result = 'PASS'
  console.log(JSON.stringify(report, null, 2))
} catch (error) {
  report.result = 'FAIL'
  report.failure = error.stack || String(error)
  throw error
} finally {
  writeFileSync(resolve(artifacts, 'publication-status.json'), JSON.stringify(report, null, 2) + '\n')
  if (browser) await browser.close()
  server.closeAllConnections()
  await new Promise((resolveClose) => server.close(resolveClose))
}
