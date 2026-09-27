import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test from 'node:test'

const source = readFileSync(new URL('../../public/branding/studio-bootstrap-v1.js', import.meta.url), 'utf8')

function boot({ path = '/public', search = '', stored = null, systemDark = false } = {}) {
  const metas = [{ media: '(prefers-color-scheme: light)', content: '#eef2f8' }, { media: '(prefers-color-scheme: dark)', content: '#0f1620' }]
  const document = {
    documentElement: { dataset: {} },
    querySelectorAll: () => metas,
  }
  const window = {
    location: { pathname: path, search },
    localStorage: { getItem: () => stored },
    matchMedia: () => ({ matches: systemDark }),
  }
  runInNewContext(source, { window, document, URLSearchParams })
  return { document, metas }
}

test('public Studio startup selects the persisted and system palette before paint', () => {
  assert.equal(boot().document.documentElement.dataset.studioBootstrap, 'cobalt')
  assert.equal(boot({ systemDark: true }).document.documentElement.dataset.studioBootstrap, 'darkroom')
  assert.equal(boot({ stored: 'light', systemDark: true }).document.documentElement.dataset.studioBootstrap, 'cobalt')
  assert.equal(boot({ stored: 'dark' }).document.documentElement.dataset.studioBootstrap, 'darkroom')
})

test('startup honors explicit legacy escape hatches and leaves console alone', () => {
  for (const search of ['?studio=0', '?legacy=1', '?studio=off']) {
    assert.deepEqual(boot({ search }).document.documentElement.dataset, {})
  }
  assert.deepEqual(boot({ path: '/console', systemDark: true }).document.documentElement.dataset, {})
})

test('startup aligns browser chrome with the initial Studio palette', () => {
  const { metas } = boot({ stored: 'dark' })
  assert.deepEqual(metas.map(({ content, media }) => ({ content, media })), [
    { content: '#0f1620', media: '' }, { content: '#0f1620', media: '' },
  ])
})
