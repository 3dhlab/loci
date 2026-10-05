import assert from 'node:assert/strict'
import test from 'node:test'
import { hasAnnotationPoint } from './annotationPoint.js'

test('a new annotation requires three captured coordinates', () => {
  for (const missing of ['', ' ', null, undefined, false, 'invalid', Infinity]) {
    assert.equal(hasAnnotationPoint({ point_x: '0', point_y: missing, point_z: '.5' }), false)
  }
  assert.equal(hasAnnotationPoint({ point_x: '', point_y: '', point_z: '' }), false)
})

test('captured and existing points may contain zero coordinates', () => {
  assert.equal(hasAnnotationPoint({ point_x: '0.000000', point_y: '0', point_z: '0.5' }), true)
  assert.equal(hasAnnotationPoint({ point_x: 0, point_y: 0, point_z: 0 }), true)
})
