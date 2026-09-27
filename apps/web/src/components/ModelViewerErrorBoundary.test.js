import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const componentSource = readFileSync(
  fileURLToPath(new URL('./ModelViewerErrorBoundary.jsx', import.meta.url)),
  'utf8',
)
const stylesSource = readFileSync(
  fileURLToPath(new URL('../styles.css', import.meta.url)),
  'utf8',
)

test('viewer error retry and help close retain the mobile touch-target contract', () => {
  assert.match(componentSource, /className="model-viewer-error-retry"/)
  assert.match(
    stylesSource,
    /\.model-viewer-help-close,\s*\.model-viewer-error-retry\s*\{\s*min-width:\s*44px;\s*min-height:\s*44px;/s,
  )
})

test('viewer errors notify the canvas host so loading and unavailable states stay exclusive', () => {
  assert.match(componentSource, /typeof this\.props\.onError === 'function'/)
  assert.match(componentSource, /this\.props\.onError\(error\)/)
})
