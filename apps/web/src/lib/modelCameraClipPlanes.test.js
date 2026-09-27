import test from 'node:test'
import assert from 'node:assert/strict'
import { Box3, BoxGeometry, Group, Mesh, MeshBasicMaterial, Vector3 } from 'three'

import { modelCameraClipPlanes, modelWorldBounds } from './modelCameraClipPlanes.js'

test('expands clip planes to cover large models around an authored camera pose', () => {
  const bounds = new Box3(new Vector3(-220, -160, -620), new Vector3(220, 185, 405))
  const planes = modelCameraClipPlanes([2.4, 1.4, 3.2], bounds, 0.1, 1000)

  assert.ok(planes.near < 0.1)
  assert.ok(planes.far >= 1000)
})

test('raises the far plane when model depth exceeds the renderer default', () => {
  const bounds = new Box3(new Vector3(-1000, -1000, -1000), new Vector3(1000, 1000, 1000))
  const planes = modelCameraClipPlanes([2.4, 1.4, 3.2], bounds, 0.1, 1000)

  assert.ok(planes.far > 1000)
})

test('keeps current clip planes for an empty model bounds box', () => {
  const planes = modelCameraClipPlanes([2.4, 1.4, 3.2], new Box3(), 0.1, 1000)

  assert.deepEqual(planes, { near: 0.1, far: 1000 })
})

test('caches local model bounds across camera movement and tracks root transforms', () => {
  const root = new Group()
  const mesh = new Mesh(new BoxGeometry(2, 2, 2), new MeshBasicMaterial())
  root.add(mesh)
  assert.equal(modelWorldBounds(root).min.x, -1)

  let childWorldUpdates = 0
  const updateChildWorldMatrix = mesh.updateWorldMatrix.bind(mesh)
  mesh.updateWorldMatrix = (...args) => { childWorldUpdates += 1; return updateChildWorldMatrix(...args) }
  root.position.x = 10
  const movedBounds = modelWorldBounds(root)

  assert.equal(childWorldUpdates, 0)
  assert.equal(movedBounds.min.x, 9)
  assert.equal(movedBounds.max.x, 11)
})

test('does not cache empty bounds and invalidates them when model children are replaced', () => {
  const root = new Group()
  assert.equal(modelWorldBounds(root).isEmpty(), true)

  const firstModel = new Mesh(new BoxGeometry(2, 2, 2), new MeshBasicMaterial())
  root.add(firstModel)
  assert.equal(modelWorldBounds(root).getSize(new Vector3()).x, 2)

  root.remove(firstModel)
  const replacementModel = new Mesh(new BoxGeometry(8, 4, 2), new MeshBasicMaterial())
  root.add(replacementModel)
  const replacementBounds = modelWorldBounds(root)

  assert.equal(replacementBounds.getSize(new Vector3()).x, 8)
  assert.equal(replacementBounds.getSize(new Vector3()).y, 4)
})
