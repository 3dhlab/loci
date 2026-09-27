import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveHydratedAnnotationId } from './evidenceSelection.js'

const sharedClipPayload = {
  focus: { source: 'clip', clip_id: 'clip-b' },
  selected_annotation: null,
  annotations: [
    { id: 'annotation-a', related_clip_ids: ['clip-a', 'clip-b'] },
    { id: 'annotation-b', related_clip_ids: ['clip-b'] },
  ],
}

test('clip share context preserves the exact selected annotation when multiple annotations share a clip', () => {
  assert.equal(resolveHydratedAnnotationId(sharedClipPayload, 'annotation-b'), 'annotation-b')
})

test('clip link without annotation context does not guess among overlapping annotations', () => {
  assert.equal(resolveHydratedAnnotationId(sharedClipPayload), '')
})

test('clip context must exist in the public payload and relate to the selected clip', () => {
  assert.equal(resolveHydratedAnnotationId(sharedClipPayload, 'not-public'), '')
  assert.equal(resolveHydratedAnnotationId(sharedClipPayload, 'annotation-a'), 'annotation-a')
  assert.equal(resolveHydratedAnnotationId({ ...sharedClipPayload, focus: { clip_id: 'unrelated' } }, 'annotation-a'), '')
})

test('server-selected annotations remain valid only when present in the public annotation list', () => {
  const payload = { ...sharedClipPayload, focus: { source: 'annotation', clip_id: null }, selected_annotation: { id: 'annotation-b' } }
  assert.equal(resolveHydratedAnnotationId(payload), 'annotation-b')
  assert.equal(resolveHydratedAnnotationId({ ...payload, annotations: [] }), '')
})

test('clip-only context cannot override an annotation-focused server response', () => {
  const payload = { ...sharedClipPayload, focus: { source: 'annotation', clip_id: null }, selected_annotation: { id: 'annotation-a' } }
  assert.equal(resolveHydratedAnnotationId(payload, 'annotation-b'), 'annotation-a')
})
