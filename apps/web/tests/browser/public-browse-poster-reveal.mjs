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
const makeObjects = (count, first = 1) => Array.from({ length: count }, (_, index) => ({
  id: `synthetic-object-${index + first}`,
  name: `Synthetic object ${index + first}`,
  project_id: 'synthetic-collection',
  description: 'A generated object for public browse layout testing.',
  evidence_url: `/evidence/objects/synthetic-object-${index + first}`,
  poster_url: `/synthetic/poster-${index + first}.png`,
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

  // A delayed page-two API response keeps page-one cards and progress near the
  // pager, then replaces both the cards and current-page announcement.
  {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })
    const page = await context.newPage()
    const pageErrors = []
    let pageTwoRequestStarted
    const pageTwoStarted = new Promise((resolveStarted) => { pageTwoRequestStarted = resolveStarted })
    let releasePageTwo
    const pageTwoRelease = new Promise((resolveRelease) => { releasePageTwo = resolveRelease })
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await page.route('**/api/v1/public/projects', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/v1/public/stats', (route) => route.fulfill({ json: { open_now_count: 6, in_preparation_count: 0 } }))
    await page.route('**/api/v1/public/objects/page*', async (route) => {
      const requestedPage = Number(new URL(route.request().url()).searchParams.get('page'))
      if (requestedPage === 2) {
        pageTwoRequestStarted()
        await pageTwoRelease
      }
      await route.fulfill({
        json: { items: makeObjects(3, requestedPage === 2 ? 4 : 1), page: requestedPage, page_size: 3, total: 6, total_pages: 2 },
      })
    })
    await page.route('**/synthetic/**', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: png }))
    await page.goto(`${origin}/public`, { waitUntil: 'domcontentloaded' })
    await page.getByText('Synthetic object 3', { exact: true }).waitFor({ state: 'visible' })
    await page.getByRole('button', { name: 'Next object page' }).click()
    await pageTwoStarted

    const status = page.locator('.public-object-page-loading')
    await status.waitFor({ state: 'visible' })
    assert.match(await status.textContent(), /Loading page 2/)
    const statusBox = await status.boundingBox()
    assert.ok(statusBox && statusBox.y >= 0 && statusBox.y < 844, 'mobile page-two progress stays inside the viewport after Next')
    assert.equal(await page.locator('.public-object-page-grid .public-library-card').count(), 3, 'page-one cards remain available during refetch')
    await page.getByRole('button', { name: 'Open evidence for Synthetic object 1' }).waitFor({ state: 'visible' })
    assert.match(await page.locator('.public-object-page-announcement').textContent(), /Page 1 of 2; loading page 2/)
    assert.equal(await page.getByRole('button', { name: 'Page 1' }).getAttribute('aria-current'), 'page')
    assert.equal(await page.getByRole('button', { name: 'Page 2' }).getAttribute('aria-current'), null)
    const spacing = await page.locator('.public-object-page-grid').evaluate((grid) => {
      const statusRect = grid.previousElementSibling.getBoundingClientRect()
      return grid.getBoundingClientRect().top - statusRect.bottom
    })
    assert.ok(spacing < 100, `loading status and previous cards stay close together (${spacing}px)`)

    releasePageTwo()
    await page.getByText('Synthetic object 6', { exact: true }).waitFor({ state: 'visible' })
    assert.equal(await page.getByText('Synthetic object 1', { exact: true }).count(), 0)
    assert.equal(await page.getByRole('button', { name: 'Page 2' }).getAttribute('aria-current'), 'page')
    assert.match(await page.locator('.public-object-page-announcement').textContent(), /Page 2 of 2/)
    assert.equal(await status.count(), 0)
    assert.deepEqual(pageErrors, [])
    await context.close()
  }
  console.log('PASS public browse posters, mobile 1–3 card sizing, and delayed page-two progress and completion')
} finally {
  if (browser) await browser.close()
  if (server.listening) await new Promise((resolveClose) => server.close(resolveClose))
}
