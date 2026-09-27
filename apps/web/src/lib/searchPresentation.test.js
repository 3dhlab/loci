import test from 'node:test'
import assert from 'node:assert/strict'
import { semanticFitTier } from './searchPresentation.js'
test('semantic fit labels agree at boundaries and reject missing scores', () => {
  assert.equal(semanticFitTier(null), null)
  assert.equal(semanticFitTier(NaN), null)
  assert.equal(semanticFitTier(0.779).tone, 'medium')
  assert.equal(semanticFitTier(0.78).tone, 'strong')
  assert.equal(semanticFitTier(0.619).tone, 'loose')
  assert.equal(semanticFitTier(0.62).tone, 'medium')
  assert.equal(semanticFitTier(2).tone, 'strong')
})
