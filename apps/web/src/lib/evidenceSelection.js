// Restore public annotation context carried alongside a clip-focused share URL.
// The public API deliberately omits selected_annotation for a clip request, so
// validate the client-only annotation_context against its allowlisted response.
export function resolveHydratedAnnotationId(payload, explicitAnnotationId = '') {
  const annotations = Array.isArray(payload?.annotations) ? payload.annotations : []
  const focusAnnotationId = payload?.selected_annotation?.id || ''
  const requestedId = payload?.focus?.source === 'clip'
    ? explicitAnnotationId || focusAnnotationId
    : focusAnnotationId
  if (!requestedId) return ''

  const annotation = annotations.find(candidate => candidate?.id === requestedId)
  if (!annotation) return ''
  const clipId = payload?.focus?.clip_id
  if (clipId && (!Array.isArray(annotation.related_clip_ids) || !annotation.related_clip_ids.includes(clipId))) {
    return ''
  }
  return annotation.id
}
