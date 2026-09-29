import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const sourceRoot = new URL('../src/', import.meta.url)
const rootEntry = readFileSync(new URL('main.jsx', sourceRoot), 'utf8')
const evidenceApp = readFileSync(new URL('evidence/EvidenceObjectApp.jsx', sourceRoot), 'utf8')
const loader = readFileSync(new URL('evidence/EvidenceLoadingState.jsx', sourceRoot), 'utf8')
const styles = readFileSync(new URL('styles.css', sourceRoot), 'utf8')
const bootstrap = readFileSync(new URL('../public/branding/studio-bootstrap-v1.js', import.meta.url), 'utf8')

test('root suspense and evidence data loading share one calm, theme-aware state', () => {
  assert.match(rootEntry, /if \(isEvidenceRoute\) \{\s*return <EvidenceLoadingState \/>/)
  assert.match(evidenceApp, /if \(loading\) \{\s*return <EvidenceLoadingState \/>/)
  assert.doesNotMatch(evidenceApp, /blocks=\{\['stage', 'detail', 'detail'\]\}/)
  assert.match(loader, /resolveStudioPaletteSync\(\)/)
  assert.match(loader, /aria-live="polite"/)
})

test('evidence loading reserves a viewport and bootstrap supports muted-light before paint', () => {
  assert.match(styles, /\.evidence-loading-screen\s*\{[^}]*min-height:\s*100vh;[^}]*min-height:\s*100svh;/s)
  assert.match(bootstrap, /\['system', 'light', 'muted-light', 'dark'\]/)
  assert.match(bootstrap, /mode === 'muted-light' \? 'muted-light'/)
})
