import { Box3, Sphere, Vector3 } from 'three'

const localBoundsByTarget = new WeakMap()

// Cache the model's bounds in its own coordinate space. OrbitControls fires
// change events during every damping frame, so only refresh the target's world
// transform there; traverse model geometry once per loaded object. Applying the
// current root world matrix keeps clipping correct if the model is repositioned.
export function modelWorldBounds(target) {
  if (!target) return null
  const cached = localBoundsByTarget.get(target)
  const childrenUnchanged = cached
    && target.children.length === cached.children.length
    && target.children.every((child, index) => child === cached.children[index])
  if (!childrenUnchanged) {
    target.updateWorldMatrix(true, true)
    const worldBounds = new Box3().setFromObject(target)
    if (worldBounds.isEmpty()) {
      // Suspense can call controls while its fallback leaves this wrapper empty.
      // Allow a later model insertion to compute and cache real bounds.
      localBoundsByTarget.delete(target)
      return worldBounds
    }
    const localBounds = worldBounds.clone().applyMatrix4(target.matrixWorld.clone().invert())
    localBoundsByTarget.set(target, { localBounds, children: [...target.children] })
    return worldBounds
  }
  target.updateWorldMatrix(true, false)
  return cached.localBounds.clone().applyMatrix4(target.matrixWorld)
}

// Expand camera depth coverage to include the model bounds while keeping the
// authored camera position and target unchanged. Large heritage scans can be
// hundreds of world units across while Three's default far plane is 1000.
export function modelCameraClipPlanes(cameraPosition, bounds, currentNear, currentFar) {
  if (!bounds || bounds.isEmpty()) {
    return { near: currentNear, far: currentFar }
  }

  const center = bounds.getCenter(new Vector3())
  const radius = bounds.getBoundingSphere(new Sphere()).radius
  const camera = Array.isArray(cameraPosition)
    ? new Vector3(...cameraPosition)
    : cameraPosition
  const centerDistance = camera.distanceTo(center)
  const closestDepth = Math.max(0.01, centerDistance - radius * 1.05)
  const farthestDepth = centerDistance + radius * 1.05

  return {
    near: Math.min(currentNear, closestDepth * 0.5),
    far: Math.max(currentFar, farthestDepth * 1.05),
  }
}
