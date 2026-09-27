import { useCallback, useEffect, useRef, useState } from 'react'

function isEmbedMessage(value) {
  return Boolean(
    value
    && typeof value === 'object'
    && typeof value.type === 'string'
    && value.type.startsWith('semanticEmbed:')
  )
}

function serialize(value) {
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function annotationRelatedClipIds(annotation) {
  if (!annotation) {
    return []
  }

  const normalized = []
  const append = (value) => {
    if (typeof value !== 'string') {
      return
    }

    const trimmed = value.trim()
    if (trimmed && !normalized.includes(trimmed)) {
      normalized.push(trimmed)
    }
  }

  if (Array.isArray(annotation.relatedClipIds)) {
    annotation.relatedClipIds.forEach(append)
  }
  append(annotation.relatedClipId)
  return normalized
}

export default function useSemanticEmbedBridge({
  objectId,
  allowedOrigin,
  chromeMode,
  manifestLoaded,
  hasInteractiveModel,
  modelReady,
  currentAnnotation,
  currentClip,
  errorMessage,
  getStateSnapshot,
  onSetAnnotation,
  onOpenClip,
  onResetView
}) {
  const isEmbedded = typeof window !== 'undefined' && window.parent && window.parent !== window
  const [initialized, setInitialized] = useState(false)
  const [targetOrigin, setTargetOrigin] = useState('')
  const announcedReadyForInitRef = useRef(false)
  const dedupeRef = useRef(new Map())

  const normalizedAllowedOrigin = (allowedOrigin || '').trim()

  const emit = useCallback((type, payload = {}, { requestId = null, targetOriginOverride = '', force = false } = {}) => {
    if (!isEmbedded) {
      return
    }

    const resolvedOrigin = targetOriginOverride || targetOrigin || normalizedAllowedOrigin || '*'
    const message = {
      type,
      version: 1,
      payload,
      ...(requestId ? { requestId } : {})
    }

    if (!force) {
      const dedupeKey = `${resolvedOrigin}:${type}:${requestId || ''}`
      const serialized = serialize(message)
      if (dedupeRef.current.get(dedupeKey) === serialized) {
        return
      }
      dedupeRef.current.set(dedupeKey, serialized)
    }

    window.parent.postMessage(message, resolvedOrigin)
  }, [isEmbedded, normalizedAllowedOrigin, targetOrigin])

  const rejectCommand = useCallback((command, reason, requestId, replyOrigin) => {
    emit(
      'semanticEmbed:commandRejected',
      {
        objectId,
        command,
        reason
      },
      {
        requestId,
        targetOriginOverride: replyOrigin,
        force: true
      }
    )
  }, [emit, objectId])

  useEffect(() => {
    dedupeRef.current.clear()
    setInitialized(false)
    setTargetOrigin('')
    announcedReadyForInitRef.current = false
  }, [objectId])

  useEffect(() => {
    if (!isEmbedded || announcedReadyForInitRef.current) {
      return
    }
    announcedReadyForInitRef.current = true
    emit(
      'semanticEmbed:readyForInit',
      {
        objectId,
        chrome: chromeMode,
        capabilities: ['setAnnotation', 'openClip', 'resetView', 'requestState']
      },
      {
        targetOriginOverride: '*',
        force: true
      }
    )
  }, [chromeMode, emit, isEmbedded, objectId])

  useEffect(() => {
    if (!isEmbedded) {
      return undefined
    }

    function handleMessage(event) {
      if (!isEmbedMessage(event.data)) {
        return
      }

      const message = event.data
      const requestId = message.requestId || null
      const payload = message.payload && typeof message.payload === 'object' ? message.payload : {}
      const command = message.type.replace('semanticEmbed:', '')
      const replyOrigin = event.origin || '*'

      if (message.type === 'semanticEmbed:init') {
        if (normalizedAllowedOrigin && replyOrigin !== normalizedAllowedOrigin) {
          rejectCommand('init', 'origin_not_allowed', requestId, replyOrigin)
          return
        }

        setTargetOrigin(replyOrigin)
        setInitialized(true)
        emit(
          'semanticEmbed:initAck',
          {
            objectId,
            chrome: chromeMode,
            manifestLoaded,
            manifestVersion: manifestLoaded ? 1 : null
          },
          {
            requestId,
            targetOriginOverride: replyOrigin,
            force: true
          }
        )

        const requestedClipId = payload.clipId || payload.id || null
        const requestedAnnotationId = payload.annotationId || payload.annotation || null
        if (requestedClipId) {
          onOpenClip?.(requestedClipId, { autoplay: Boolean(payload.autoplay), source: 'init' })
          return
        }
        if (requestedAnnotationId) {
          onSetAnnotation?.(requestedAnnotationId, { autoplay: Boolean(payload.autoplay), source: 'init' })
        }
        return
      }

      if (!initialized) {
        rejectCommand(command, 'not_initialized', requestId, replyOrigin)
        return
      }

      if ((targetOrigin || normalizedAllowedOrigin) && replyOrigin !== (targetOrigin || normalizedAllowedOrigin)) {
        rejectCommand(command, 'origin_mismatch', requestId, replyOrigin)
        return
      }

      if (message.type === 'semanticEmbed:setAnnotation') {
        const annotationId = payload.annotationId || payload.id || null
        if (!annotationId || !onSetAnnotation?.(annotationId, { autoplay: Boolean(payload.autoplay), source: 'bridge' })) {
          rejectCommand('setAnnotation', 'annotation_not_found', requestId, replyOrigin)
          return
        }
        emit(
          'semanticEmbed:commandAck',
          { objectId, command: 'setAnnotation', state: getStateSnapshot() },
          { requestId, targetOriginOverride: replyOrigin, force: true }
        )
        return
      }

      if (message.type === 'semanticEmbed:openClip') {
        const clipId = payload.clipId || payload.id || null
        if (!clipId || !onOpenClip?.(clipId, { autoplay: Boolean(payload.autoplay), source: 'bridge' })) {
          rejectCommand('openClip', 'clip_not_found', requestId, replyOrigin)
          return
        }
        emit(
          'semanticEmbed:commandAck',
          { objectId, command: 'openClip', state: getStateSnapshot() },
          { requestId, targetOriginOverride: replyOrigin, force: true }
        )
        return
      }

      if (message.type === 'semanticEmbed:resetView') {
        onResetView?.()
        emit(
          'semanticEmbed:commandAck',
          { objectId, command: 'resetView', state: getStateSnapshot() },
          { requestId, targetOriginOverride: replyOrigin, force: true }
        )
        return
      }

      if (message.type === 'semanticEmbed:requestState') {
        emit(
          'semanticEmbed:state',
          { objectId, state: getStateSnapshot() },
          { requestId, targetOriginOverride: replyOrigin, force: true }
        )
      }
    }

    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [
    chromeMode,
    emit,
    getStateSnapshot,
    initialized,
    isEmbedded,
    manifestLoaded,
    normalizedAllowedOrigin,
    objectId,
    onOpenClip,
    onResetView,
    onSetAnnotation,
    rejectCommand,
    targetOrigin
  ])

  useEffect(() => {
    if (!initialized || !manifestLoaded) {
      return
    }
    emit('semanticEmbed:ready', { objectId, state: getStateSnapshot() })
  }, [emit, getStateSnapshot, initialized, manifestLoaded, objectId])

  useEffect(() => {
    if (!initialized) {
      return
    }

    emit('semanticEmbed:loadProgress', {
      objectId,
      hasInteractiveModel,
      manifestLoaded,
      modelReady: hasInteractiveModel ? modelReady : true,
      phase: !manifestLoaded
        ? 'loading_manifest'
        : hasInteractiveModel && !modelReady
          ? 'loading_interactive_model'
          : 'interactive_model_ready',
      progressPercent: !manifestLoaded
        ? 18
        : hasInteractiveModel && !modelReady
          ? 76
          : 100
    })
  }, [emit, hasInteractiveModel, initialized, manifestLoaded, modelReady, objectId])

  useEffect(() => {
    if (!initialized || !currentAnnotation) {
      return
    }

    const relatedClipIds = annotationRelatedClipIds(currentAnnotation)

    emit('semanticEmbed:annotationSelect', {
      objectId,
      annotationId: currentAnnotation.id,
      relatedClipId: relatedClipIds[0] || null,
      relatedClipIds,
      relatedPublicationIds: currentAnnotation.relatedPublicationIds || [],
      relatedProjectIds: currentAnnotation.relatedProjectIds || [],
      relatedLocationIds: currentAnnotation.relatedLocationIds || []
    })
  }, [currentAnnotation, emit, initialized, objectId])

  useEffect(() => {
    if (!initialized) {
      return
    }

    if (currentClip) {
      emit('semanticEmbed:clipOpen', {
        objectId,
        clipId: currentClip.id,
        title: currentClip.title,
        startTime: currentClip.startTime,
        endTime: currentClip.endTime
      })
      return
    }

    emit('semanticEmbed:clipClose', { objectId })
  }, [currentClip, emit, initialized, objectId])

  useEffect(() => {
    if (!initialized || !errorMessage) {
      return
    }

    emit('semanticEmbed:error', {
      objectId,
      message: errorMessage
    })
  }, [emit, errorMessage, initialized, objectId])
}