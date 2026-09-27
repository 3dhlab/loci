/* Studio P3 — model auto-focus geometry (pure, framework-free, unit-testable).
 *
 * The Studio prefers an annotation's stored `camera_json` ({position,target}).
 * When that is absent, this module computes a centering pose from the pin's
 * point (+ optional surface normal) and the model's bounding sphere, so the
 * camera frames the pin head-on.
 *
 * Orientation matches the documented derivation in
 * docs/frontend/studio-frontend-implementation-surface-2026-06-25.md §1c
 * (`pitch = atan2(dy, dz)`, `yaw = atan2(-dx, hypot(dy, dz))` for a unit surface
 * dir): placing the camera at `point + normal * distance` and looking at `point`
 * yields exactly that pitch/yaw, without the sign pitfalls of applying Euler
 * angles by hand. `directionToPitchYaw` is exported so a test can assert the
 * equivalence.
 */

const DEFAULT_VIEW_DIRECTION = [2.8, 2.2, 2.8]
const FIT_NEAR_MULTIPLIER = 1.8
const FIT_DISTANCE_PADDING = 1.35
// Studio P4.5 — annotation framing that keeps the WHOLE object in frame.
// `ANNOTATION_FIT_BIAS` is how far the look-at target leans from the object
// center toward the selected pin (0 = pure whole-object center, 1 = pin-centered);
// `ANNOTATION_FIT_MARGIN` is extra breathing room so tall objects never clip.
const ANNOTATION_FIT_BIAS = 0.35
const ANNOTATION_FIT_MARGIN = 1.12

function isFiniteVec3(v) {
  return Array.isArray(v) && v.length === 3 && v.every((n) => Number.isFinite(n))
}

function length3([x, y, z]) {
  return Math.sqrt(x * x + y * y + z * z)
}

function normalize3(v) {
  const len = length3(v)
  if (!Number.isFinite(len) || len === 0) {
    return null
  }
  return [v[0] / len, v[1] / len, v[2] / len]
}

/**
 * Fit distance from the model's bounding-sphere radius and the camera FOV
 * (degrees). Mirrors the existing box-fit in ModelCanvas so computed focus and
 * the default fit stay visually consistent.
 */
export function computeFitDistance(radius, fovDeg) {
  const safeRadius = Number.isFinite(radius) && radius > 0 ? radius : 1
  const fov = ((Number.isFinite(fovDeg) && fovDeg > 0 ? fovDeg : 45) * Math.PI) / 180
  const base = Math.max(safeRadius / Math.tan(fov / 2), safeRadius * FIT_NEAR_MULTIPLIER)
  return base * FIT_DISTANCE_PADDING
}

/**
 * The unit direction from the pin point toward the camera. Prefers the surface
 * normal; falls back to the outward direction (point - center); finally a fixed
 * three-quarter view. Always returns a unit vector.
 */
export function resolveFocusDirection({ point, normal, center }) {
  if (isFiniteVec3(normal)) {
    const n = normalize3(normal)
    if (n) {
      return n
    }
  }
  if (isFiniteVec3(point) && isFiniteVec3(center)) {
    const outward = normalize3([point[0] - center[0], point[1] - center[1], point[2] - center[2]])
    if (outward) {
      return outward
    }
  }
  return normalize3(DEFAULT_VIEW_DIRECTION)
}

/**
 * Compute a {position, target} camera view that frames `point` head-on.
 * Returns null when there is no usable point (caller then keeps its default fit).
 */
export function computeFocusCameraView({ point, normal, center, radius, fovDeg = 45 } = {}) {
  if (!isFiniteVec3(point)) {
    return null
  }
  const direction = resolveFocusDirection({ point, normal, center })
  if (!direction) {
    return null
  }
  const distance = computeFitDistance(radius, fovDeg)
  return {
    position: [
      point[0] + direction[0] * distance,
      point[1] + direction[1] * distance,
      point[2] + direction[2] * distance,
    ],
    target: [point[0], point[1], point[2]],
  }
}

/**
 * Studio P4.5 — annotation-aware WHOLE-OBJECT fit. Unlike computeFocusCameraView
 * (which centers on the pin and can push a tall object out of frame), this frames
 * the entire model: the look-at target only leans from the object `center` toward
 * the pin by `bias`, and the camera sits back far enough to contain the whole
 * bounding sphere (radius grown by the center→target offset) plus a `margin`.
 * The result is that the selected detail is emphasized while the object stays
 * centered and fully visible. Falls back to the point-centered view when no
 * usable `center` is available. Pure/side-effect-free for unit testing.
 */
export function computeAnnotationFitView({
  point,
  normal,
  center,
  radius,
  fovDeg = 45,
  bias = ANNOTATION_FIT_BIAS,
  margin = ANNOTATION_FIT_MARGIN,
} = {}) {
  if (!isFiniteVec3(center)) {
    return computeFocusCameraView({ point, normal, center, radius, fovDeg })
  }
  const direction = resolveFocusDirection({ point, normal, center })
  if (!direction) {
    return null
  }
  const clampedBias = Math.min(Math.max(Number(bias) || 0, 0), 1)
  const hasPoint = isFiniteVec3(point)
  const target = hasPoint
    ? [
        center[0] + (point[0] - center[0]) * clampedBias,
        center[1] + (point[1] - center[1]) * clampedBias,
        center[2] + (point[2] - center[2]) * clampedBias,
      ]
    : [center[0], center[1], center[2]]
  const offset = length3([target[0] - center[0], target[1] - center[1], target[2] - center[2]])
  const safeRadius = Number.isFinite(radius) && radius > 0 ? radius : 1
  const safeMargin = Number.isFinite(margin) && margin > 0 ? margin : 1
  const distance = computeFitDistance(safeRadius + offset, fovDeg) * safeMargin
  return {
    position: [
      target[0] + direction[0] * distance,
      target[1] + direction[1] * distance,
      target[2] + direction[2] * distance,
    ],
    target,
  }
}

/**
 * Documented pitch/yaw for a unit surface direction (radians). Exposed for tests
 * to verify the placement above matches the spec's derivation.
 */
export function directionToPitchYaw([dx, dy, dz]) {
  return {
    pitch: Math.atan2(dy, dz),
    yaw: Math.atan2(-dx, Math.hypot(dy, dz)),
  }
}
