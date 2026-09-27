/* Unit coverage for the Studio P3 auto-focus geometry (lib/focusCamera).
 * Runs on Node's built-in runner: `node --test`. Pure math, no DOM/three. */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  computeFitDistance,
  resolveFocusDirection,
  computeFocusCameraView,
  computeAnnotationFitView,
  directionToPitchYaw,
} from './focusCamera.js'

const approx = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps

test('computeFitDistance scales with radius and honors the padding', () => {
  const d1 = computeFitDistance(1, 45)
  const d2 = computeFitDistance(2, 45)
  assert.ok(d1 > 0)
  assert.ok(approx(d2, d1 * 2), `${d2} ~ ${d1 * 2}`)
  // Falls back to safe defaults for bad input.
  assert.ok(computeFitDistance(0, 45) > 0)
  assert.ok(computeFitDistance(NaN, NaN) > 0)
})

test('resolveFocusDirection prefers a (normalized) surface normal', () => {
  const dir = resolveFocusDirection({ point: [1, 0, 0], normal: [0, 0, 5], center: [0, 0, 0] })
  assert.deepEqual(dir.map((n) => Number(n.toFixed(6))), [0, 0, 1])
})

test('resolveFocusDirection falls back to outward (point - center) when no normal', () => {
  const dir = resolveFocusDirection({ point: [0, 3, 0], normal: null, center: [0, 0, 0] })
  assert.deepEqual(dir.map((n) => Number(n.toFixed(6))), [0, 1, 0])
})

test('resolveFocusDirection falls back to a fixed three-quarter view when degenerate', () => {
  const dir = resolveFocusDirection({ point: [0, 0, 0], normal: [0, 0, 0], center: [0, 0, 0] })
  assert.equal(dir.length, 3)
  assert.ok(approx(Math.hypot(...dir), 1), 'must be unit length')
})

test('computeFocusCameraView returns null without a usable point', () => {
  assert.equal(computeFocusCameraView({ point: null }), null)
  assert.equal(computeFocusCameraView({ point: [1, NaN, 0] }), null)
})

test('computeFocusCameraView places camera along the normal at fit distance, targeting the point', () => {
  const point = [1, 2, 3]
  const view = computeFocusCameraView({ point, normal: [0, 0, 1], center: [0, 0, 0], radius: 1, fovDeg: 45 })
  const distance = computeFitDistance(1, 45)
  assert.deepEqual(view.target, point)
  // position = point + unitNormal * distance
  assert.ok(approx(view.position[0], 1, 1e-6))
  assert.ok(approx(view.position[1], 2, 1e-6))
  assert.ok(approx(view.position[2], 3 + distance, 1e-6))
})

test('computeAnnotationFitView biases the target between center and pin (not pin-centered)', () => {
  const center = [0, 0, 0]
  const point = [0, 10, 0] // pin high on a tall object
  const view = computeAnnotationFitView({ point, normal: [0, 0, 1], center, radius: 6, fovDeg: 45, bias: 0.35 })
  // Target leans toward the pin but stays near center (0.35 of the way up).
  assert.ok(approx(view.target[1], 3.5, 1e-6), `target.y=${view.target[1]}`)
  assert.equal(view.target[0], 0)
  assert.equal(view.target[2], 0)
})

test('computeAnnotationFitView keeps the whole object in frame for a tall object (no clip)', () => {
  const center = [0, 0, 0]
  const radius = 6
  const point = [0, 10, 0]
  const fovDeg = 45
  const view = computeAnnotationFitView({ point, normal: [0, 0, 1], center, radius, fovDeg, bias: 0.35, margin: 1.12 })
  // Half-vertical extent visible at the camera distance must cover the farthest
  // point of the bounding sphere measured from the (biased) look-at target.
  const dist = Math.hypot(
    view.position[0] - view.target[0],
    view.position[1] - view.target[1],
    view.position[2] - view.target[2],
  )
  const halfExtentVisible = dist * Math.tan((fovDeg * Math.PI) / 180 / 2)
  const targetOffset = Math.hypot(...view.target) // |target - center|
  assert.ok(halfExtentVisible >= radius + targetOffset, `visible ${halfExtentVisible} >= ${radius + targetOffset}`)
})

test('computeAnnotationFitView falls back to the point-centered view without a center', () => {
  const view = computeAnnotationFitView({ point: [1, 2, 3], normal: [0, 0, 1], center: null, radius: 1, fovDeg: 45 })
  const ref = computeFocusCameraView({ point: [1, 2, 3], normal: [0, 0, 1], center: null, radius: 1, fovDeg: 45 })
  assert.deepEqual(view.target, ref.target)
})

test('computed placement matches the documented pitch/yaw derivation (§1c)', () => {
  // For a unit surface dir, the camera sits at point + dir*distance looking back
  // at point; its view direction is -dir, whose pitch/yaw must equal the spec's
  // derivation applied to dir.
  const dir = resolveFocusDirection({ point: [0, 0, 0], normal: [0.3, -0.4, 0.866], center: [1, 1, 1] })
  const spec = directionToPitchYaw(dir)
  // pitch = atan2(dy, dz), yaw = atan2(-dx, hypot(dy,dz))
  assert.ok(approx(spec.pitch, Math.atan2(dir[1], dir[2])))
  assert.ok(approx(spec.yaw, Math.atan2(-dir[0], Math.hypot(dir[1], dir[2]))))
})
