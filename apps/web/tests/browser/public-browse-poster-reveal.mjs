import assert from 'node:assert/strict'
import { createReadStream, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const webRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const distRoot = resolve(webRoot, 'dist-ci')
assert.ok(existsSync(resolve(distRoot, 'index.html')), 'run the CI production build before the browse browser regression')
const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' }
const server = createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname
  let filePath = resolve(distRoot, `.${pathname}`)
  if (!filePath.startsWith(`${distRoot}${sep}`) || !existsSync(filePath)) filePath = resolve(distRoot, 'index.html')
  createReadStream(filePath).on('error', () => {
    res.writeHead(404).end()
  }).on('open', () => {
    res.setHeader('Content-Type', contentTypes[extname(filePath)] || 'application/octet-stream')
  }).pipe(res)
})

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/fS8AAAAASUVORK5CYII=', 'base64')
const makeObjects = (count) => Array.from({ length: count }, (_, index) => ({
  id: `synthetic-object-${index + 1}`,
  name: `Synthetic object ${index + 1}`,
  project_id: 'synthetic-collection',
  description: 'A generated object for public browse layout testing.',
  evidence_url: `/evidence/objects/synthetic-object-${index + 1}`,
  poster_url: `/synthetic/poster-${index + 1}.png`,
}))

let browser
try {
  await new Promise((resolveListen, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolveListen) })
  browser = await chromium.launch({ headless: true })
  const origin = `http://127.0.0.1:${server.address().port}`

  // A slow, broken, and prompt poster share one page. The slow poster exceeds
  // the eight second card fallback timer, then succeeds and replaces its fallback.
  {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })
    const page = await context.newPage()
    const pageErrors = []
    let slowRequestStarted
    const slowStarted = new Promise((resolveStarted) => { slowRequestStarted = resolveStarted })
    let releaseSlow
    const slowRelease = new Promise((resolveRelease) => { releaseSlow = resolveRelease })
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await page.route('**/api/v1/public/projects', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/v1/public/stats', (route) => route.fulfill({ json: { open_now_count: 3, in_preparation_count: 0 } }))
    await page.route('**/api/v1/public/objects/page*', (route) => route.fulfill({
      json: { items: makeObjects(3), page: 1, page_size: 3, total: 3, total_pages: 1 },
    }))
    await page.route('**/synthetic/poster-1.png*', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: png }))
    await page.route('**/synthetic/poster-2.png*', (route) => route.fulfill({ status: 404, body: 'synthetic missing poster' }))
    await page.route('**/synthetic/poster-3.png*', async (route) => {
      slowRequestStarted()
      await slowRelease
      await route.fulfill({ status: 200, contentType: 'image/png', body: png })
    })

    await page.goto(`${origin}/public`, { waitUntil: 'domcontentloaded' })
    const grid = page.locator('.public-object-page-grid')
    await grid.waitFor({ state: 'visible' })
    await slowStarted
    for (const index of [1, 2, 3]) {
      const card = page.locator('.public-library-card').nth(index - 1)
      await card.locator('h3', { hasText: `Synthetic object ${index}` }).waitFor({ state: 'visible' })
      await card.getByRole('button', { name: `Open evidence for Synthetic object ${index}` }).waitFor({ state: 'visible' })
    }
    assert.equal(await page.locator('.public-library-card').count(), 3, 'all cards remain available while a poster request is pending')
    await page.locator('.public-library-card').nth(1).getByText('Poster unavailable').waitFor({ state: 'visible' })
    await page.locator('.public-library-card').nth(2).getByText('Poster loading').waitFor({ state: 'visible' })

    await page.locator('.public-library-card').nth(2).getByText(/taking longer to load/).waitFor({ state: 'visible', timeout: 10000 })
    assert.equal(await page.locator('.public-library-card').count(), 3, 'the timeout fallback does not hide object names or actions')
    releaseSlow()
    await page.waitForFunction(() => {
      const image = document.querySelector('.public-library-card:nth-child(3) .public-library-card-poster-image')
      return image?.complete && image.naturalWidth > 0 && image.closest('.public-library-card-poster-frame')?.classList.contains('is-loaded')
    })
    assert.equal(await page.locator('.public-library-card').nth(2).getByText('Poster loading').count(), 0,
      'a poster that loads after timeout replaces its fallback')
    assert.deepEqual(pageErrors, [])
    await context.close()
  }

  // The mobile grid height follows the number of rendered objects.
  for (const count of [1, 2, 3]) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })
    const page = await context.newPage()
    await page.route('**/api/v1/public/projects', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/v1/public/stats', (route) => route.fulfill({ json: { open_now_count: count, in_preparation_count: 0 } }))
    await page.route('**/api/v1/public/objects/page*', (route) => route.fulfill({
      json: { items: makeObjects(count), page: 1, page_size: count, total: count, total_pages: 1 },
    }))
    await page.route('**/synthetic/**', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: png }))
    await page.goto(`${origin}/public`, { waitUntil: 'domcontentloaded' })
    await page.getByText(`Synthetic object ${count}`, { exact: true }).waitFor({ state: 'visible' })
    await page.waitForFunction(() => [...document.querySelectorAll('.public-library-card-poster-image')].every((image) => image.complete && image.naturalWidth > 0))
    const sizes = await page.locator('.public-object-page-grid').evaluate((node) => {
      const cards = [...node.children]
      const grid = node.getBoundingClientRect()
      const cardHeights = cards.map((card) => card.getBoundingClientRect().height)
      const gap = parseFloat(getComputedStyle(node).rowGap) || 0
      return { gridHeight: grid.height, expectedHeight: cardHeights.reduce((sum, height) => sum + height, 0) + gap * Math.max(0, cards.length - 1) }
    })
    assert.equal(await page.locator('.public-library-card').count(), count)
    assert.ok(Math.abs(sizes.gridHeight - sizes.expectedHeight) <= 2,
      `${count} mobile cards: grid height ${sizes.gridHeight}px should match its rendered rows ${sizes.expectedHeight}px`)
    await context.close()
  }
  console.log('PASS public browse cards remain usable during poster delay/failure, late recovery works, and mobile grid sizes match 1–3 cards')
} finally {
  if (browser) await browser.close()
  if (server.listening) await new Promise((resolveClose) => server.close(resolveClose))
}
