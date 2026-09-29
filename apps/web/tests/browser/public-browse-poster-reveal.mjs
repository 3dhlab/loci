import assert from 'node:assert/strict'
import { createReadStream } from 'node:fs'
import { createServer } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const webRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const distRoot = resolve(webRoot, 'dist')
const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' }
const server = createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname
  let filePath = resolve(distRoot, `.${pathname}`)
  if (!filePath.startsWith(`${distRoot}${sep}`)) filePath = resolve(distRoot, 'index.html')
  createReadStream(filePath).on('error', () => {
    res.writeHead(404).end()
  }).on('open', () => {
    res.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream')
  }).pipe(res)
})

const objects = Array.from({ length: 3 }, (_, index) => ({
  id: `synthetic-object-${index + 1}`,
  name: `Synthetic object ${index + 1}`,
  project_id: 'synthetic-collection',
  description: 'A generated object for public browse layout testing.',
  evidence_url: `/evidence/objects/synthetic-object-${index + 1}`,
  poster_url: '/synthetic/poster.png',
}))

let browser
try {
  await new Promise((resolveListen, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolveListen) })
  browser = await chromium.launch({ headless: true })
  const origin = `http://127.0.0.1:${server.address().port}`

  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce' })
    const page = await context.newPage()
    const pageErrors = []
    let notifyPosterRequest
    const posterRequestStarted = new Promise((resolveStarted) => { notifyPosterRequest = resolveStarted })
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await page.route('**/api/v1/public/projects', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/v1/public/stats', (route) => route.fulfill({ json: { open_now_count: 3, in_preparation_count: 0 } }))
    await page.route('**/api/v1/public/objects/page*', (route) => route.fulfill({
      json: { items: objects, page: 1, page_size: 3, total: 3, total_pages: 1 },
    }))
    await page.route('**/synthetic/poster.png*', async (route) => {
      notifyPosterRequest()
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 600))
      await route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/fS8AAAAASUVORK5CYII=', 'base64') })
    })

    await page.goto(`${origin}/public`, { waitUntil: 'domcontentloaded' })
    const loading = page.locator('.public-browse-loading')
    await loading.waitFor({ state: 'visible' })
    await posterRequestStarted
    await page.waitForTimeout(120)
    assert.equal(await loading.isVisible(), true, 'browse stays in its loading state while the poster is delayed')
    assert.equal(await page.locator('.public-library-loading-card, .skeleton-card-block.card').count(), 0,
      'browse must not flash three skeleton cards')
    assert.equal(await page.locator('.public-library-card').count(), 0, 'object cards wait for their posters to decode')
    const loadingHeight = await loading.evaluate((node) => node.getBoundingClientRect().height)
    await page.getByText('Synthetic object 1', { exact: true }).waitFor({ state: 'visible' })
    const grid = page.locator('.public-object-page-grid')
    await grid.waitFor({ state: 'visible' })
    await page.waitForFunction(() => [...document.querySelectorAll('.public-library-card-poster-image')].length === 3
      && [...document.querySelectorAll('.public-library-card-poster-image')].every((image) => image.complete && image.naturalWidth > 0))
    const gridHeight = await grid.evaluate((node) => node.getBoundingClientRect().height)
    assert.ok(Math.abs(gridHeight - loadingHeight) <= 2,
      `${viewport.width}px viewport: loading reserve ${loadingHeight}px must match card grid ${gridHeight}px`)
    assert.equal(await page.locator('.public-library-card').count(), 3)
    assert.deepEqual(pageErrors, [])
    await context.close()
  }
  console.log('PASS public browse poster reveal and stable grid at desktop and mobile viewports')
} finally {
  if (browser) await browser.close()
  if (server.listening) await new Promise((resolveClose) => server.close(resolveClose))
}
