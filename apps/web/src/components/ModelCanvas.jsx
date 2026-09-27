import { Suspense, memo, useEffect, useMemo, useRef, useState } from 'react'
import { Html, OrbitControls, useGLTF } from '@react-three/drei'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Box3, Spherical, SRGBColorSpace, Vector3 } from 'three'

import BrandMotion from './BrandMotion'
import ModelViewerErrorBoundary from './ModelViewerErrorBoundary'
import { computeAnnotationFitView } from '../lib/focusCamera'
import { modelCameraClipPlanes, modelWorldBounds } from '../lib/modelCameraClipPlanes'

const MARKER_OFFSET = 0.018
const LABEL_OFFSET = 0.045
const MOBILE_CANVAS_MEDIA_QUERY = '(max-width: 760px)'
const REDUCED_MOTION_MEDIA_QUERY = '(prefers-reduced-motion: reduce)'
// iOS Safari can hang WebGL context/texture upload indefinitely without ever
// throwing, so ModelViewerErrorBoundary never fires and the loading overlay
// would otherwise stay up forever. If the scene has not become ready within this
// window we surface a recoverable retry instead of an endless spinner.
const MODEL_LOAD_STALL_TIMEOUT_MS = 25000
// Bounded sway envelope (radians) around the held focus pose — small enough that
// the focused pin never leaves frame. Idle turntable speed is radians/second.
const SWAY_AZIMUTH_RAD = 0.05
const SWAY_POLAR_RAD = 0.02
const IDLE_TURNTABLE_RAD_PER_S = 0.08
// P4.6: after ANY user interaction (drag/zoom/pan/touch/keyboard) ambient motion
// pauses this long before resuming — and resumes anchored to the user's current
// pose, never snapping back to the precomputed focus pose.
const AMBIENT_RESUME_DELAY_MS = 8000
// A 'change' this recently after our own controls.update() is our motion (or its
// damping echo), not the user — ignored so ambient never pauses itself.
const AMBIENT_SELF_CHANGE_WINDOW_MS = 60
// Keyboard viewer navigation keys that should pause ambient motion.
const AMBIENT_NAV_KEYS = new Set([
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'PageUp', 'PageDown', 'Home', 'End', '+', '-', '=', '_',
])
// Pin facing thresholds (dot of pin world-normal vs view-to-camera direction).
const PIN_FACING_VISIBLE = 0.2
const PIN_FACING_HIDDEN = -0.1
// P4.5: back-facing pins fade MODESTLY but never disappear and never lose pointer
// events — labels must stay visible and selectable during rotation (review #2).
const PIN_MIN_OPACITY = 0.4

const modelResourceRefCounts = new Map()

function retainModelResources(modelUrl) {
  modelResourceRefCounts.set(modelUrl, (modelResourceRefCounts.get(modelUrl) || 0) + 1)
}

function cloneTextureResource(texture, textureClones) {
  if (!texture?.isTexture) {
    return texture
  }

  if (textureClones.has(texture)) {
    return textureClones.get(texture)
  }

  const textureClone = texture.clone()
  textureClone.colorSpace = texture.colorSpace
  textureClone.needsUpdate = true
  textureClones.set(texture, textureClone)
  return textureClone
}

function cloneMaterialResource(material, materialClones, textureClones) {
  if (!material) {
    return material
  }

  if (materialClones.has(material)) {
    return materialClones.get(material)
  }

  const materialClone = material.clone()
  Object.entries(materialClone).forEach(([key, value]) => {
    if (!value?.isTexture) {
      return
    }
    materialClone[key] = cloneTextureResource(value, textureClones)
  })
  materialClones.set(material, materialClone)
  return materialClone
}

function createManagedSceneClone(scene) {
  const clonedScene = scene.clone(true)
  const sourceRenderables = []
  const clonedRenderables = []
  const geometryClones = new Map()
  const materialClones = new Map()
  const textureClones = new Map()

  scene.traverse((node) => {
    if (node.isMesh || node.isLine || node.isPoints) {
      sourceRenderables.push(node)
    }
  })

  clonedScene.traverse((node) => {
    if (node.isMesh || node.isLine || node.isPoints) {
      clonedRenderables.push(node)
    }
  })

  clonedRenderables.forEach((clonedNode, index) => {
    const sourceNode = sourceRenderables[index]
    if (!sourceNode) {
      return
    }

    if (sourceNode.geometry) {
      if (!geometryClones.has(sourceNode.geometry)) {
        geometryClones.set(sourceNode.geometry, sourceNode.geometry.clone())
      }
      clonedNode.geometry = geometryClones.get(sourceNode.geometry)
    }

    const sourceMaterials = Array.isArray(sourceNode.material) ? sourceNode.material : [sourceNode.material]
    const clonedMaterials = sourceMaterials.map((material) => cloneMaterialResource(material, materialClones, textureClones))
    clonedNode.material = Array.isArray(sourceNode.material) ? clonedMaterials : clonedMaterials[0]
  })

  return clonedScene
}

function disposeMaterialResources(material, seenTextures, seenMaterials) {
  if (!material || seenMaterials.has(material)) {
    return
  }

  seenMaterials.add(material)
  Object.values(material).forEach((value) => {
    if (!value?.isTexture || seenTextures.has(value)) {
      return
    }
    seenTextures.add(value)
    value.dispose()
  })
  material.dispose?.()
}

function disposeSceneResources(scene) {
  const seenGeometries = new Set()
  const seenMaterials = new Set()
  const seenTextures = new Set()

  scene.traverse((node) => {
    if (!node.isMesh) {
      return
    }

    if (node.geometry && !seenGeometries.has(node.geometry)) {
      seenGeometries.add(node.geometry)
      node.geometry.dispose()
    }

    const materials = Array.isArray(node.material) ? node.material : [node.material]
    materials.forEach((material) => disposeMaterialResources(material, seenTextures, seenMaterials))
  })
}

function releaseModelResources(modelUrl, scene) {
  const nextCount = (modelResourceRefCounts.get(modelUrl) || 0) - 1
  if (nextCount > 0) {
    modelResourceRefCounts.set(modelUrl, nextCount)
    return
  }

  modelResourceRefCounts.delete(modelUrl)
  disposeSceneResources(scene)
  useGLTF.clear(modelUrl)
}

function roundTransformValue(value) {
  return Number(value.toFixed(4))
}

function normalizeCameraView(cameraView) {
  const position = Array.isArray(cameraView?.position) ? cameraView.position : null
  const target = Array.isArray(cameraView?.target) ? cameraView.target : null
  if (!position || !target || position.length !== 3 || target.length !== 3) {
    return null
  }

  const normalizedPosition = position.map((value) => Number(value))
  const normalizedTarget = target.map((value) => Number(value))
  if (normalizedPosition.some((value) => !Number.isFinite(value)) || normalizedTarget.some((value) => !Number.isFinite(value))) {
    return null
  }

  return {
    position: normalizedPosition,
    target: normalizedTarget
  }
}

function captureCameraView(camera, controls) {
  return {
    position: [
      roundTransformValue(camera.position.x),
      roundTransformValue(camera.position.y),
      roundTransformValue(camera.position.z)
    ],
    target: [
      roundTransformValue(controls.target.x),
      roundTransformValue(controls.target.y),
      roundTransformValue(controls.target.z)
    ]
  }
}

function updateCameraClipPlanesForModel(camera, target) {
  if (!target) {
    return
  }
  const bounds = modelWorldBounds(target)
  const planes = modelCameraClipPlanes(camera.position, bounds, camera.near, camera.far)
  if (planes.near === camera.near && planes.far === camera.far) {
    return
  }
  camera.near = planes.near
  camera.far = planes.far
  camera.updateProjectionMatrix()
}

function ModelLoadingOverlay({ active = true, interactive = false, eyebrow, title, message, action = null, backgroundColor }) {
  return (
    <div
      className={`model-canvas-loading-overlay ${active ? 'active' : 'inactive'}${interactive ? ' interactive' : ''}`.trim()}
      role={active ? 'status' : undefined}
      aria-live={active ? 'polite' : undefined}
      aria-busy={active || undefined}
      aria-hidden={active ? undefined : true}
      style={backgroundColor ? { background: backgroundColor } : undefined}
    >
      <div className="model-canvas-loading-copy">
        <div className="model-canvas-loading-mark" aria-hidden="true">
          <BrandMotion name="pivot" size={84} className="brand-motion-glow" />
        </div>
        <div className="model-canvas-loading-text">
          <span className="model-canvas-loading-eyebrow">{eyebrow}</span>
          <strong>{title}</strong>
          <p>{message}</p>
          {action ? action : (
            <div className="model-canvas-loading-bar" aria-hidden="true">
              <span />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function CanvasLoadingFallback({ title }) {
  return null
}

function WebGLContextMonitor({ onContextLost, onContextRestored }) {
  const { gl } = useThree()

  useEffect(() => {
    const canvas = gl.domElement
    const handleContextLost = (event) => {
      event.preventDefault()
      onContextLost?.()
    }
    const handleContextRestored = () => {
      onContextRestored?.()
    }

    canvas.addEventListener('webglcontextlost', handleContextLost)
    canvas.addEventListener('webglcontextrestored', handleContextRestored)

    return () => {
      canvas.removeEventListener('webglcontextlost', handleContextLost)
      canvas.removeEventListener('webglcontextrestored', handleContextRestored)
    }
  }, [gl, onContextLost, onContextRestored])

  useEffect(() => {
    return () => {
      try {
        gl.renderLists?.dispose?.()
      } catch {
        // Best-effort renderer cleanup for mobile Safari object switches.
      }
      try {
        gl.dispose?.()
      } catch {
        // Ignore renderer disposal errors during teardown.
      }
    }
  }, [gl])

  return null
}

function ClickableModel({ modelUrl, placementMode, onSurfacePick, onReady }) {
  const { scene } = useGLTF(modelUrl)
  const clonedScene = useMemo(() => createManagedSceneClone(scene), [scene])

  useEffect(() => {
    retainModelResources(modelUrl)
    return () => {
      disposeSceneResources(clonedScene)
      releaseModelResources(modelUrl, scene)
    }
  }, [clonedScene, modelUrl, scene])

  useEffect(() => {
    clonedScene.traverse((node) => {
      if (!node.isMesh) {
        return
      }

      const materials = Array.isArray(node.material) ? node.material : [node.material]
      for (const material of materials) {
        if (!material) {
          continue
        }
        if (material.map) {
          material.map.colorSpace = SRGBColorSpace
        }
        material.needsUpdate = true
      }
    })
  }, [clonedScene])

  useEffect(() => {
    const animationFrameId = window.requestAnimationFrame(() => {
      onReady?.()
    })
    return () => window.cancelAnimationFrame(animationFrameId)
  }, [clonedScene, modelUrl, onReady])

  return (
    <primitive
      object={clonedScene}
      onClick={(event) => {
        if (!placementMode || typeof onSurfacePick !== 'function') {
          return
        }
        event.stopPropagation()
        const normal = event.face?.normal?.clone()
        if (normal) {
          normal.transformDirection(event.object.matrixWorld).normalize()
        }
        onSurfacePick({
          point: {
            x: event.point.x,
            y: event.point.y,
            z: event.point.z
          },
          normal: normal
            ? {
                x: normal.x,
                y: normal.y,
                z: normal.z
              }
            : null
        })
      }}
    />
  )
}

function AnnotationMarkers({ annotations, selectedAnnotationId, showAnnotationLabels, onSelectAnnotation, registryRef, fadeEnabled = false }) {
  const [hoveredAnnotationId, setHoveredAnnotationId] = useState('')
  const labelButtonRefs = useRef(new Map())

  function registryEntry(id) {
    if (!registryRef) {
      return null
    }
    let entry = registryRef.current.get(id)
    if (!entry) {
      entry = { group: null, button: null, normal: null }
      registryRef.current.set(id, entry)
    }
    return entry
  }

  // When fade is off (legacy/embed), make sure no stale inline opacity lingers on
  // the pin buttons from a prior Studio render of the shared canvas.
  useEffect(() => {
    if (fadeEnabled) {
      return
    }
    labelButtonRefs.current.forEach((button) => {
      if (button instanceof HTMLElement) {
        button.style.opacity = ''
        button.style.pointerEvents = ''
      }
    })
  }, [fadeEnabled])

  function focusAnnotationButton(index) {
    const annotationId = annotations[index]?.id
    if (!annotationId) {
      return
    }

    const button = labelButtonRefs.current.get(annotationId)
    if (!(button instanceof HTMLElement)) {
      return
    }

    button.focus()
  }

  function handleAnnotationKeyDown(index, event) {
    if (!annotations.length) {
      return
    }

    let nextIndex = null
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        nextIndex = Math.min(annotations.length - 1, index + 1)
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        nextIndex = Math.max(0, index - 1)
        break
      case 'Home':
        nextIndex = 0
        break
      case 'End':
        nextIndex = annotations.length - 1
        break
      default:
        return
    }

    event.preventDefault()
    focusAnnotationButton(nextIndex)
  }

  return annotations.map((annotation, index) => {
    const selected = annotation.id === selectedAnnotationId
    const needsReview = annotation.review_status === 'REVIEW_REQUIRED'
    const hovered = hoveredAnnotationId === annotation.id
    const normalX = Number(annotation.normal_x)
    const normalY = Number(annotation.normal_y)
    const normalZ = Number(annotation.normal_z)
    const hasNormal = Number.isFinite(normalX) && Number.isFinite(normalY) && Number.isFinite(normalZ)
    const basePosition = hasNormal
      ? [
          Number(annotation.point_x) + normalX * MARKER_OFFSET,
          Number(annotation.point_y) + normalY * MARKER_OFFSET,
          Number(annotation.point_z) + normalZ * MARKER_OFFSET
        ]
      : [Number(annotation.point_x), Number(annotation.point_y), Number(annotation.point_z)]
    const showLabel = showAnnotationLabels
    const labelY = LABEL_OFFSET
    const labelClassName = [
      'model-pin-callout',
      selected ? 'selected' : '',
      needsReview ? 'review' : '',
      hovered ? 'hovered' : ''
    ].filter(Boolean).join(' ')

    return (
      <group
        key={annotation.id}
        position={basePosition}
        ref={(node) => {
          const entry = registryEntry(annotation.id)
          if (!entry) {
            return
          }
          if (node) {
            entry.group = node
            entry.normal = hasNormal ? new Vector3(normalX, normalY, normalZ) : null
          } else {
            entry.group = null
          }
        }}
      >
        {showLabel ? (
          <Html position={[0, labelY, 0]}>
            <button
              ref={(node) => {
                const entry = registryEntry(annotation.id)
                if (node) {
                  labelButtonRefs.current.set(annotation.id, node)
                  if (entry) entry.button = node
                  return
                }

                labelButtonRefs.current.delete(annotation.id)
                if (entry) entry.button = null
              }}
              type="button"
              className={labelClassName}
              aria-current={selected ? 'true' : undefined}
              onMouseEnter={() => setHoveredAnnotationId(annotation.id)}
              onMouseLeave={() => setHoveredAnnotationId((current) => (current === annotation.id ? '' : current))}
              onKeyDown={(event) => handleAnnotationKeyDown(index, event)}
              onClick={(event) => {
                event.stopPropagation()
                onSelectAnnotation?.(annotation)
              }}
            >
              <div className="model-pin-callout-line" />
              <div className="model-pin-label">{annotation.title}</div>
            </button>
          </Html>
        ) : null}
      </group>
    )
  })
}

function focusAnnotationPoint(focusAnnotation) {
  if (!focusAnnotation) {
    return null
  }
  const point = [Number(focusAnnotation.point_x), Number(focusAnnotation.point_y), Number(focusAnnotation.point_z)]
  return point.every((n) => Number.isFinite(n)) ? point : null
}

function focusAnnotationNormal(focusAnnotation) {
  if (!focusAnnotation) {
    return null
  }
  const normal = [Number(focusAnnotation.normal_x), Number(focusAnnotation.normal_y), Number(focusAnnotation.normal_z)]
  return normal.every((n) => Number.isFinite(n)) ? normal : null
}

// Compute the intended camera pose ({position,target,near,far}) for the current
// view. Pure of side effects so it can be hard-applied (legacy/embed) OR
// published for the eased Studio motion path.
function computeCameraPose({ camera, controls, normalizedCameraView, focusAnnotation, targetRef }) {
  if (normalizedCameraView) {
    let modelBounds = null
    if (targetRef?.current) {
      targetRef.current.updateWorldMatrix(true, true)
      modelBounds = new Box3().setFromObject(targetRef.current)
    }
    const clipPlanes = modelCameraClipPlanes(
      normalizedCameraView.position,
      modelBounds,
      camera.near,
      camera.far,
    )
    return {
      position: [...normalizedCameraView.position],
      target: [...normalizedCameraView.target],
      ...clipPlanes,
    }
  }

  // Computed focus (Studio P4.5): frame the WHOLE object with the look-at target
  // biased toward the selected pin. Uses the bounding SPHERE radius (half the box
  // diagonal, not half the largest edge) so tall/narrow objects are never clipped,
  // and computeAnnotationFitView backs the camera off far enough to keep the whole
  // object in frame (surface doc §1c + P4.5 review).
  const focusPoint = focusAnnotationPoint(focusAnnotation)
  if (focusPoint && targetRef?.current) {
    targetRef.current.updateWorldMatrix(true, true)
    const focusBox = new Box3().setFromObject(targetRef.current)
    if (!focusBox.isEmpty()) {
      const center = focusBox.getCenter(new Vector3())
      const size = focusBox.getSize(new Vector3())
      const radius = size.length() * 0.5 || 1
      const focusView = computeAnnotationFitView({
        point: focusPoint,
        normal: focusAnnotationNormal(focusAnnotation),
        center: [center.x, center.y, center.z],
        radius,
        fovDeg: camera.fov,
      })
      if (focusView) {
        const viewDistance = Math.hypot(
          focusView.position[0] - focusView.target[0],
          focusView.position[1] - focusView.target[1],
          focusView.position[2] - focusView.target[2],
        ) || radius
        return {
          position: [...focusView.position],
          target: [...focusView.target],
          near: Math.max(0.1, viewDistance / 100),
          far: Math.max(1000, viewDistance * 10),
        }
      }
    }
  }

  // Default: fit the whole model in a three-quarter view.
  if (targetRef?.current) {
    targetRef.current.updateWorldMatrix(true, true)
    const box = new Box3().setFromObject(targetRef.current)
    if (!box.isEmpty()) {
      const center = box.getCenter(new Vector3())
      const size = box.getSize(new Vector3())
      // Bounding-sphere radius (half the box diagonal): aspect-robust for
      // tall/diagonal objects, matching the focused-fit branch above.
      const radius = size.length() * 0.5 || 1
      const fov = (camera.fov * Math.PI) / 180
      const fitDistance = Math.max(radius / Math.tan(fov / 2), radius * 1.8)
      const viewDirection = new Vector3(2.8, 2.2, 2.8).normalize()
      const nextPosition = center.clone().add(viewDirection.multiplyScalar(fitDistance * 1.35))
      return {
        position: [nextPosition.x, nextPosition.y, nextPosition.z],
        target: [center.x, center.y, center.z],
        near: Math.max(0.1, fitDistance / 100),
        far: Math.max(1000, fitDistance * 10),
      }
    }
  }
  return null
}

function CameraController({
  appliedCameraView,
  focusAnnotation,
  controlsRef,
  onCameraViewChange,
  targetRef,
  viewKey,
  smoothMotion = false,
  desiredPoseRef = null,
  poseVersionRef = null,
}) {
  const { camera } = useThree()
  const appliedKeyRef = useRef('')
  const normalizedCameraView = useMemo(() => normalizeCameraView(appliedCameraView), [appliedCameraView])

  useEffect(() => {
    const nextKey = `${JSON.stringify(normalizedCameraView)}::${viewKey}`
    if (appliedKeyRef.current === nextKey) {
      return
    }
    appliedKeyRef.current = nextKey

    const animationFrameId = window.requestAnimationFrame(() => {
      const controls = controlsRef.current
      if (!controls) {
        return
      }
      const pose = computeCameraPose({ camera, controls, normalizedCameraView, focusAnnotation, targetRef })
      if (!pose) {
        return
      }

      if (smoothMotion && desiredPoseRef && poseVersionRef) {
        // Publish the desired pose; StudioCameraMotion eases the camera to it so
        // selection never snaps.
        desiredPoseRef.current = pose
        poseVersionRef.current += 1
        return
      }

      // Legacy/embed (and reduced-motion): apply immediately, unchanged.
      camera.position.set(...pose.position)
      controls.target.set(...pose.target)
      camera.lookAt(...pose.target)
      camera.near = pose.near
      camera.far = pose.far
      camera.updateProjectionMatrix()
      controls.update()
      if (typeof onCameraViewChange === 'function') {
        onCameraViewChange(captureCameraView(camera, controls))
      }
    })

    return () => window.cancelAnimationFrame(animationFrameId)
  }, [camera, controlsRef, focusAnnotation, normalizedCameraView, onCameraViewChange, targetRef, viewKey, smoothMotion, desiredPoseRef, poseVersionRef])

  return (
    <OrbitControls
      ref={controlsRef}
      makeDefault
      enablePan={true}
      enableRotate={true}
      enableZoom={true}
      enableDamping={smoothMotion}
      dampingFactor={0.08}
      onChange={() => {
        updateCameraClipPlanesForModel(camera, targetRef.current)
        if (typeof onCameraViewChange !== 'function' || !controlsRef.current) {
          return
        }
        onCameraViewChange(captureCameraView(camera, controlsRef.current))
      }}
      onEnd={() => {
        updateCameraClipPlanesForModel(camera, targetRef.current)
        if (typeof onCameraViewChange !== 'function' || !controlsRef.current) {
          return
        }
        onCameraViewChange(captureCameraView(camera, controlsRef.current))
      }}
    />
  )
}

// Eased Studio camera motion (opt-in, desktop, non-reduced-motion). Owns all
// camera movement in Studio: it damps the camera toward the published desired
// pose (so a moment selection glides in, never snaps), then adds a bounded sway
// while a moment is focused, or a slow turntable when idle. User gestures take
// over (detected via OrbitControls 'start'/'end'), and the resting pose re-
// anchors from wherever the user left off — no fight, no twitch. A polar clamp
// keeps the object from lying flat or flipping overhead.
function StudioCameraMotion({ enabled, controlsRef, hasFocus, desiredPoseRef, poseVersionRef, paused = false }) {
  const { camera, gl, invalidate } = useThree()
  const homeRef = useRef(null) // { theta, phi, radius, target: Vector3 }
  const turntableRef = useRef(0)
  const lastVersionRef = useRef(-1)
  const resumeAtRef = useRef(0) // performance.now() ms; ambient may run once now >= this
  const lastSelfUpdateAtRef = useRef(0) // when WE last drove controls.update()

  const nowMs = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : 0)

  // Any genuine user interaction pauses ambient motion and (re)arms the idle
  // timer. We cover the full input surface: OrbitControls start/change/end plus
  // raw DOM pointerdown/wheel/touch and keyboard viewer-navigation. The 'change'
  // listener is time-guarded so our OWN per-frame moves (and their damping echo)
  // never pause the motion — only real user changes do.
  useEffect(() => {
    if (!enabled) {
      return undefined
    }
    const controls = controlsRef.current
    const dom = gl?.domElement
    const bump = () => {
      resumeAtRef.current = nowMs() + AMBIENT_RESUME_DELAY_MS
      homeRef.current = null // re-anchor from wherever the user leaves the camera
      invalidate()
    }
    const onControlsChange = () => {
      if (nowMs() - lastSelfUpdateAtRef.current > AMBIENT_SELF_CHANGE_WINDOW_MS) {
        bump()
      }
    }
    const onKeyDown = (event) => {
      if (AMBIENT_NAV_KEYS.has(event.key)) {
        bump()
      }
    }
    controls?.addEventListener?.('start', bump)
    controls?.addEventListener?.('change', onControlsChange)
    controls?.addEventListener?.('end', bump)
    dom?.addEventListener?.('pointerdown', bump)
    dom?.addEventListener?.('wheel', bump, { passive: true })
    dom?.addEventListener?.('touchstart', bump, { passive: true })
    dom?.addEventListener?.('touchmove', bump, { passive: true })
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', onKeyDown)
    }
    return () => {
      controls?.removeEventListener?.('start', bump)
      controls?.removeEventListener?.('change', onControlsChange)
      controls?.removeEventListener?.('end', bump)
      dom?.removeEventListener?.('pointerdown', bump)
      dom?.removeEventListener?.('wheel', bump)
      dom?.removeEventListener?.('touchstart', bump)
      dom?.removeEventListener?.('touchmove', bump)
      if (typeof window !== 'undefined') {
        window.removeEventListener('keydown', onKeyDown)
      }
    }
  }, [controlsRef, enabled, gl, invalidate])

  useFrame((state, delta) => {
    if (!enabled) {
      return
    }
    const controls = controlsRef.current
    if (!controls) {
      return
    }

    // A newly published desired pose (moment selection / fit change) always wins:
    // anchor to it and glide in, cancelling any idle pause. This is the ONE case
    // where the camera moves toward the precomputed pose.
    const version = poseVersionRef ? poseVersionRef.current : 0
    if (lastVersionRef.current !== version) {
      lastVersionRef.current = version
      const pose = desiredPoseRef ? desiredPoseRef.current : null
      const target = pose ? new Vector3(...pose.target) : controls.target.clone()
      const posVec = pose ? new Vector3(...pose.position) : camera.position.clone()
      const sph = new Spherical().setFromVector3(posVec.clone().sub(target))
      homeRef.current = { theta: sph.theta, phi: sph.phi, radius: sph.radius, target }
      turntableRef.current = 0
      resumeAtRef.current = 0
      if (pose) {
        camera.near = pose.near ?? camera.near
        camera.far = pose.far ?? camera.far
        camera.updateProjectionMatrix()
      }
    }

    // Fully paused while a modal (viewer help / citation) is open: don't touch the
    // camera, and keep the idle timer armed so motion only returns ~8s AFTER the
    // modal closes, re-anchored to the user's current pose.
    if (paused) {
      resumeAtRef.current = nowMs() + AMBIENT_RESUME_DELAY_MS
      homeRef.current = null
      return
    }
    // Idle window after a user gesture: leave the camera alone. OrbitControls
    // (with damping) settles the user's own motion on its own frames — we must
    // not fight it or snap it back.
    if (nowMs() < resumeAtRef.current) {
      return
    }

    invalidate() // keep the frameloop warm while ambient motion is active

    // Resume anchored to the CURRENT user pose (never snap back to the focus pose).
    if (!homeRef.current) {
      const target = controls.target.clone()
      const posVec = camera.position.clone()
      const sph = new Spherical().setFromVector3(posVec.clone().sub(target))
      homeRef.current = { theta: sph.theta, phi: sph.phi, radius: sph.radius, target }
      turntableRef.current = 0
    }

    const home = homeRef.current
    const t = state.clock.elapsedTime
    let theta = home.theta
    let phi = home.phi
    if (hasFocus) {
      theta += Math.sin(t * 0.5) * SWAY_AZIMUTH_RAD
      phi += Math.sin(t * 0.37) * SWAY_POLAR_RAD
    } else {
      turntableRef.current += IDLE_TURNTABLE_RAD_PER_S * Math.min(delta, 0.05)
      theta += turntableRef.current
    }
    // Keep a natural elevation: never fully flat (lying down) or straight overhead.
    phi = Math.max(0.45, Math.min(1.35, phi))

    const desiredSpherical = new Spherical(home.radius, phi, theta)
    desiredSpherical.makeSafe()
    const desiredPos = new Vector3().setFromSpherical(desiredSpherical).add(home.target)

    // Frame-rate-independent damping (~0.1/frame at 60fps) for a smooth glide.
    const damp = 1 - Math.pow(0.0009, Math.min(delta, 0.05))
    camera.position.lerp(desiredPos, damp)
    controls.target.lerp(home.target, damp)
    camera.lookAt(controls.target)
    // update() re-derives OrbitControls' spherical from the (lerped) camera, so
    // internal state stays in sync and the next user drag never jumps. Mark the
    // time so the 'change' this dispatches is recognized as ours, not the user's.
    lastSelfUpdateAtRef.current = nowMs()
    controls.update()
  })

  return null
}

function PinFacingController({ enabled, markerRef, controlsRef }) {
  const { camera } = useThree()
  useFrame(() => {
    const markers = markerRef.current
    if (!enabled || !markers || markers.size === 0) {
      return
    }
    const forward = new Vector3()
    camera.getWorldDirection(forward)
    for (const entry of markers.values()) {
      const { group, button, normal } = entry
      if (!group || !(button instanceof HTMLElement)) {
        continue
      }
      group.updateWorldMatrix(true, false)
      let facing = 1
      if (normal) {
        const worldNormal = normal.clone().transformDirection(group.matrixWorld).normalize()
        // worldNormal points away from the surface; a pin facing the camera has a
        // normal pointing opposite the camera's forward vector -> negative dot.
        facing = -worldNormal.dot(forward)
      }
      let facingRatio
      if (facing >= PIN_FACING_VISIBLE) {
        facingRatio = 1
      } else if (facing <= PIN_FACING_HIDDEN) {
        facingRatio = 0
      } else {
        facingRatio = (facing - PIN_FACING_HIDDEN) / (PIN_FACING_VISIBLE - PIN_FACING_HIDDEN)
      }
      // Map to [PIN_MIN_OPACITY, 1] — a back-facing pin dims but never vanishes,
      // and pointer events stay active so the label is always clickable (review #2).
      const opacity = PIN_MIN_OPACITY + (1 - PIN_MIN_OPACITY) * facingRatio
      button.style.opacity = String(opacity)
      button.style.pointerEvents = 'auto'
    }
  })
  return null
}

// P4.5 — in-view "?" help. A compact circular button in the viewer's bottom
// corner opens an in-view controls overlay (Voyager-style navigation help +
// Sketchfab control expectations). Static React text only: no remote assets, no
// dangerouslySetInnerHTML. Escape closes and focus returns to the opener.
function ViewerHelp({ onOpenChange }) {
  const [open, setOpen] = useState(false)
  const closeRef = useRef(null)
  const openerRef = useRef(null)

  // Report open/close so the host can hide the annotation pins while the overlay
  // is up (they must not float above it) and pause ambient camera motion.
  useEffect(() => {
    if (typeof onOpenChange === 'function') {
      onOpenChange(open)
    }
  }, [open, onOpenChange])

  // If the component unmounts while open (e.g. the loading overlay reappears),
  // make sure the host clears the "help open" state so pins return.
  useEffect(() => () => {
    if (typeof onOpenChange === 'function') {
      onOpenChange(false)
    }
  }, [onOpenChange])

  useEffect(() => {
    if (!open) {
      return undefined
    }
    const handleKey = (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        setOpen(false)
        openerRef.current?.focus()
      }
    }
    window.addEventListener('keydown', handleKey)
    closeRef.current?.focus()
    return () => window.removeEventListener('keydown', handleKey)
  }, [open])

  return (
    <div className="model-viewer-help">
      <button
        type="button"
        ref={openerRef}
        className="model-viewer-help-button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="How to use the 3D viewer"
        title="How to use the 3D viewer"
        onClick={() => setOpen((value) => !value)}
      >
        <span aria-hidden="true">?</span>
      </button>
      {open ? (
        <div
          className="model-viewer-help-overlay"
          role="dialog"
          aria-modal="false"
          aria-label="3D viewer controls"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              setOpen(false)
              openerRef.current?.focus()
            }
          }}
        >
          <div className="model-viewer-help-panel">
            <div className="model-viewer-help-head">
              <h3>Viewer controls</h3>
              <button
                type="button"
                ref={closeRef}
                className="model-viewer-help-close"
                aria-label="Close viewer help"
                onClick={() => {
                  setOpen(false)
                  openerRef.current?.focus()
                }}
              >
                Close
              </button>
            </div>
            <dl className="model-viewer-help-list">
              <div>
                <dt>Rotate / orbit</dt>
                <dd>Left-drag, or drag with one finger on touch.</dd>
              </div>
              <div>
                <dt>Pan</dt>
                <dd>Right-drag, Shift + drag, or drag with two fingers.</dd>
              </div>
              <div>
                <dt>Zoom</dt>
                <dd>Scroll the mouse wheel, or pinch on touch.</dd>
              </div>
              <div>
                <dt>Select a moment</dt>
                <dd>Click an annotation label to watch its evidence moment.</dd>
              </div>
              <div>
                <dt>Keyboard</dt>
                <dd>Tab to a label, then press Enter or Space to select; arrow keys move between labels.</dd>
              </div>
            </dl>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function ModelCanvas({
  modelUrl,
  modelTransform,
  defaultCameraView,
  annotations,
  selectedAnnotationId,
  placementMode,
  editMode = false,
  cameraViewKey = 'default',
  canvasDpr = undefined,
  preferLowPower = false,
  showAnnotationLabels = true,
  showAutoLoadingOverlay = true,
  isLoading = false,
  loadingEyebrow = '3D view',
  loadingTitle = 'Preparing 3D view...',
  loadingMessage = 'Loading model geometry and annotation positions.',
  emptyTitle = 'No 3D model',
  emptyMessage = 'Upload or select a .glb model to begin.',
  onSceneReadyChange,
  onSurfacePick,
  onTransformChange,
  onCameraViewChange,
  onSelectAnnotation,
  // Studio P3: an optional focused moment. When present its stored `camera_json`
  // ({position,target}) is preferred for framing; otherwise the camera is
  // computed to frame the pin head-on from its point/normal (lib/focusCamera).
  // Null by default, so legacy/embed callers behave exactly as before.
  focusAnnotation = null,
  // Studio-only, opt-in (default off so legacy/embed/mobile are unchanged):
  // bounded sway when focused + slow idle turntable; auto-disabled under
  // prefers-reduced-motion. And fade pins whose surface faces away from camera.
  enableAmbientMotion = false,
  fadePinsWhenAway = false,
  // P4.5 Studio-only, opt-in: a circular "?" help button in the viewer corner
  // that opens an in-view controls overlay. Off by default so legacy/embed/editor
  // viewers are unchanged.
  showViewerHelp = false,
  // P4.6 Studio-only: when a citation modal is open the host sets this so ambient
  // camera motion stays paused underneath it (matches the viewer-help pause).
  suspendAmbientMotion = false,
  // WebGL clear color. Default keeps the legacy/embed cream; Studio passes a
  // lighter warm-white so the object stage reads closer to the video still.
  backgroundColor = '#f5efe6'
}) {
  const resolvedBackgroundColor = backgroundColor || '#f5efe6'
  const [viewerHelpOpen, setViewerHelpOpen] = useState(false)
  const shellRef = useRef(null)
  const transformGroupRef = useRef(null)
  const modelGeometryRef = useRef(null)
  const orbitControlsRef = useRef(null)
  const latestTransformRef = useRef({
    position_x: 0,
    position_y: 0,
    position_z: 0,
    rotation_x: 0,
    rotation_y: 0,
    rotation_z: 0
  })
  const dragStateRef = useRef(null)
  const pinRegistryRef = useRef(new Map())
  const desiredPoseRef = useRef(null)
  const poseVersionRef = useRef(0)
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false)
  const [sceneReady, setSceneReady] = useState(false)
  const [canvasResetKey, setCanvasResetKey] = useState(0)
  const [contextStatus, setContextStatus] = useState('')
  const [stalled, setStalled] = useState(false)
  const [viewerError, setViewerError] = useState(false)
  const [isMobileViewport, setIsMobileViewport] = useState(() => (
    typeof window !== 'undefined' ? window.matchMedia(MOBILE_CANVAS_MEDIA_QUERY).matches : false
  ))

  const position = [
    Number(modelTransform?.position_x ?? 0),
    Number(modelTransform?.position_y ?? 0),
    Number(modelTransform?.position_z ?? 0)
  ]
  const rotation = [
    (Number(modelTransform?.rotation_x ?? 0) * Math.PI) / 180,
    (Number(modelTransform?.rotation_y ?? 0) * Math.PI) / 180,
    (Number(modelTransform?.rotation_z ?? 0) * Math.PI) / 180
  ]

  function endTransformDrag() {
    dragStateRef.current = null
    if (orbitControlsRef.current) {
      orbitControlsRef.current.enabled = true
    }
  }

  useEffect(() => {
    latestTransformRef.current = {
      position_x: Number(modelTransform?.position_x ?? 0),
      position_y: Number(modelTransform?.position_y ?? 0),
      position_z: Number(modelTransform?.position_z ?? 0),
      rotation_x: Number(modelTransform?.rotation_x ?? 0),
      rotation_y: Number(modelTransform?.rotation_y ?? 0),
      rotation_z: Number(modelTransform?.rotation_z ?? 0)
    }
  }, [modelTransform])

  useEffect(() => {
    setSceneReady(false)
    setContextStatus('')
    setViewerError(false)
  }, [modelUrl])

  // Graceful model-load stall detection. Arm a timer only while a model load is
  // genuinely in flight (a model URL is set, the scene is not ready yet, and we
  // are not in a context-loss recovery cycle). If the scene never becomes ready
  // within the window we flip `stalled` so the overlay can offer a retry rather
  // than spinning forever. Re-arms on modelUrl / canvasResetKey change; clears on
  // ready, context error, and unmount.
  useEffect(() => {
    if (!modelUrl || sceneReady || contextStatus) {
      setStalled(false)
      return undefined
    }
    setStalled(false)
    const timerId = window.setTimeout(() => {
      setStalled(true)
    }, MODEL_LOAD_STALL_TIMEOUT_MS)
    return () => window.clearTimeout(timerId)
  }, [modelUrl, sceneReady, contextStatus, canvasResetKey])

  function handleStalledRetry() {
    setStalled(false)
    setContextStatus('')
    setViewerError(false)
    setSceneReady(false)
    useGLTF.clear(modelUrl)
    setCanvasResetKey((current) => current + 1)
  }

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined
    }

    const mediaQuery = window.matchMedia(MOBILE_CANVAS_MEDIA_QUERY)
    const handleViewportChange = (event) => {
      setIsMobileViewport(event.matches)
    }

    setIsMobileViewport(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleViewportChange)
      return () => mediaQuery.removeEventListener('change', handleViewportChange)
    }

    mediaQuery.addListener(handleViewportChange)
    return () => mediaQuery.removeListener(handleViewportChange)
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined
    }
    const mediaQuery = window.matchMedia(REDUCED_MOTION_MEDIA_QUERY)
    const handleChange = (event) => setPrefersReducedMotion(event.matches)
    setPrefersReducedMotion(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    }
    mediaQuery.addListener(handleChange)
    return () => mediaQuery.removeListener(handleChange)
  }, [])

  useEffect(() => {
    onSceneReadyChange?.(sceneReady)
  }, [onSceneReadyChange, sceneReady])

  const resolvedCanvasDpr = canvasDpr ?? (isMobileViewport ? [1, 1.5] : undefined)
  const resolvedPreferLowPower = Boolean(preferLowPower || isMobileViewport)
  const resolvedLoadingEyebrow = contextStatus ? '3D recovery' : loadingEyebrow
  const resolvedLoadingTitle = contextStatus === 'lost'
    ? '3D context interrupted'
    : contextStatus === 'restoring'
      ? 'Restoring 3D view...'
      : loadingTitle
  const resolvedLoadingMessage = contextStatus === 'lost'
    ? 'This device reclaimed the WebGL context. Restoring the current model view now.'
    : contextStatus === 'restoring'
      ? 'Reloading model geometry after the graphics context was restored.'
      : loadingMessage
  const showLoadingOverlay = Boolean(!viewerError && (isLoading || contextStatus || (showAutoLoadingOverlay && modelUrl && !sceneReady)))
  const showStalledOverlay = Boolean(!viewerError && stalled && modelUrl && !sceneReady && !contextStatus)
  const annotationsVisible = Boolean(sceneReady && !isLoading && !contextStatus)
  const visibleAnnotations = annotationsVisible ? annotations : []

  // Studio P3 focus + motion derivation.
  // P4.5: a focused annotation always resolves to null so the CameraController
  // computes a whole-object-fit pose (computeAnnotationFitView). Stored per-pin
  // camera_json is intentionally NOT used for focus framing — it was the source of
  // stale/over-tight views that clipped tall objects with tall geometry.
  // With no focus, the idle default view is still honored.
  const resolvedAppliedCameraView = focusAnnotation?.id ? null : defaultCameraView
  const hasFocus = Boolean(focusAnnotation?.id)
  const ambientMotionActive = Boolean(enableAmbientMotion && !prefersReducedMotion && sceneReady)
  const pinFadeActive = Boolean(fadePinsWhenAway && sceneReady)
  // Ambient motion needs continuous frames; otherwise keep the existing
  // demand/always behavior (mobile + low-power stay on demand).
  const resolvedFrameloop = ambientMotionActive ? 'always' : (resolvedPreferLowPower ? 'demand' : 'always')

  function handleCameraViewUpdate(nextView) {
    onCameraViewChange?.(nextView)

    const shell = shellRef.current
    if (!(shell instanceof HTMLElement)) {
      return
    }

    shell.dispatchEvent(new CustomEvent('model-canvas-camera-view-change', { detail: nextView, bubbles: true }))
  }

  if (!modelUrl) {
    if (isLoading) {
      return (
        <div className="model-canvas-shell loading" style={{ background: resolvedBackgroundColor }}>
          <div className="model-canvas-stage model-canvas-stage-placeholder" style={{ background: resolvedBackgroundColor }} />
          <ModelLoadingOverlay active={true} eyebrow={resolvedLoadingEyebrow} title={resolvedLoadingTitle} message={resolvedLoadingMessage} backgroundColor={resolvedBackgroundColor} />
        </div>
      )
    }

    return (
      <div className="model-canvas-empty" style={{ background: resolvedBackgroundColor }}>
        <div className="model-canvas-empty-copy">
          <strong>{emptyTitle}</strong>
          <p>{emptyMessage}</p>
        </div>
      </div>
    )
  }

  return (
    <div
      ref={shellRef}
      className={`model-canvas-shell ${showLoadingOverlay ? 'loading' : ''}`.trim()}
      style={{ background: resolvedBackgroundColor }}
      onPointerDownCapture={(event) => {
        if (!editMode || event.button !== 0 || typeof onTransformChange !== 'function') {
          return
        }

        const interactionMode = event.shiftKey ? 'move' : event.altKey ? 'rotate' : null
        if (!interactionMode) {
          return
        }

        dragStateRef.current = {
          mode: interactionMode,
          x: event.clientX,
          y: event.clientY
        }
        if (orbitControlsRef.current) {
          orbitControlsRef.current.enabled = false
        }
        event.preventDefault()
      }}
      onPointerMove={(event) => {
        if (!editMode || !dragStateRef.current || typeof onTransformChange !== 'function') {
          return
        }

        const dx = event.clientX - dragStateRef.current.x
        const dy = event.clientY - dragStateRef.current.y
        dragStateRef.current = { ...dragStateRef.current, x: event.clientX, y: event.clientY }

        const nextTransform = { ...latestTransformRef.current }
        if (dragStateRef.current.mode === 'move') {
          nextTransform.position_x = roundTransformValue(nextTransform.position_x + dx * 0.02)
          nextTransform.position_y = roundTransformValue(nextTransform.position_y - dy * 0.02)
        } else {
          nextTransform.rotation_x = roundTransformValue(nextTransform.rotation_x + dy * 0.35)
          nextTransform.rotation_y = roundTransformValue(nextTransform.rotation_y + dx * 0.35)
        }

        latestTransformRef.current = nextTransform
        onTransformChange(nextTransform)
        event.preventDefault()
      }}
      onPointerUp={endTransformDrag}
      onPointerCancel={endTransformDrag}
      onPointerLeave={endTransformDrag}
    >
      <div className="model-canvas-stage">
        <ModelViewerErrorBoundary
          key={`viewer-boundary-${canvasResetKey}`}
          onError={() => {
            setStalled(false)
            setViewerError(true)
          }}
          onReset={() => {
            setViewerError(false)
            useGLTF.clear(modelUrl)
            setCanvasResetKey((current) => current + 1)
          }}
        >
        <Canvas
          key={`${canvasResetKey}`}
          camera={{ position: [2.8, 2.2, 2.8], fov: 45 }}
          dpr={resolvedCanvasDpr}
          frameloop={resolvedFrameloop}
          resize={{ scroll: false, offsetSize: true }}
          style={{ width: '100%', height: '100%' }}
          gl={{
            antialias: !resolvedPreferLowPower,
            powerPreference: resolvedPreferLowPower ? 'low-power' : 'high-performance'
          }}
        >
          <color attach="background" args={[resolvedBackgroundColor]} />
          <ambientLight intensity={1.05} />
          <hemisphereLight intensity={0.95} groundColor="#c8b49a" color="#fff6eb" />
          <directionalLight position={[5, 6, 6]} intensity={1.35} />
          <directionalLight position={[-4, 3, -4]} intensity={0.55} />
          <WebGLContextMonitor
            onContextLost={() => {
              setContextStatus('lost')
              setSceneReady(false)
            }}
            onContextRestored={() => {
              setContextStatus('restoring')
              setSceneReady(false)
              setCanvasResetKey((current) => current + 1)
            }}
          />

          <group ref={transformGroupRef} position={position} rotation={rotation}>
            <Suspense fallback={<CanvasLoadingFallback title={resolvedLoadingTitle} />}>
              <group ref={modelGeometryRef}>
                <ClickableModel
                  modelUrl={modelUrl}
                  placementMode={placementMode}
                  onSurfacePick={onSurfacePick}
                  onReady={() => {
                    setContextStatus('')
                    setSceneReady(true)
                  }}
                />
              </group>
            </Suspense>
            <AnnotationMarkers
              annotations={visibleAnnotations}
              selectedAnnotationId={selectedAnnotationId}
              showAnnotationLabels={showAnnotationLabels && annotationsVisible && !viewerHelpOpen}
              onSelectAnnotation={onSelectAnnotation}
              registryRef={pinRegistryRef}
              fadeEnabled={pinFadeActive}
            />
          </group>
            <CameraController
              appliedCameraView={resolvedAppliedCameraView}
              focusAnnotation={focusAnnotation}
              controlsRef={orbitControlsRef}
              onCameraViewChange={handleCameraViewUpdate}
              targetRef={modelGeometryRef}
              viewKey={`${cameraViewKey}:${focusAnnotation?.id || 'none'}:${sceneReady ? 'ready' : 'loading'}`}
              smoothMotion={ambientMotionActive}
              desiredPoseRef={desiredPoseRef}
              poseVersionRef={poseVersionRef}
            />
            <StudioCameraMotion
              enabled={ambientMotionActive}
              controlsRef={orbitControlsRef}
              hasFocus={hasFocus}
              desiredPoseRef={desiredPoseRef}
              poseVersionRef={poseVersionRef}
              paused={viewerHelpOpen || suspendAmbientMotion}
            />
            <PinFacingController
              enabled={pinFadeActive}
              markerRef={pinRegistryRef}
              controlsRef={orbitControlsRef}
            />
        </Canvas>
        </ModelViewerErrorBoundary>
      </div>

      {showViewerHelp && !showLoadingOverlay && !viewerError ? <ViewerHelp onOpenChange={setViewerHelpOpen} /> : null}

      {showStalledOverlay ? (
        <ModelLoadingOverlay
          active={true}
          interactive={true}
          eyebrow={resolvedLoadingEyebrow}
          title="3D model is taking too long"
          message="It may not be supported on this device, or the connection stalled. The rest of this page still works."
          backgroundColor={resolvedBackgroundColor}
          action={(
            <button type="button" className="model-canvas-load-action" onClick={handleStalledRetry}>
              Try again
            </button>
          )}
        />
      ) : (
        <ModelLoadingOverlay active={showLoadingOverlay} eyebrow={resolvedLoadingEyebrow} title={resolvedLoadingTitle} message={resolvedLoadingMessage} backgroundColor={resolvedBackgroundColor} />
      )}
    </div>
  )
}

const MemoizedModelCanvas = memo(ModelCanvas)
MemoizedModelCanvas.displayName = 'ModelCanvas'

export default MemoizedModelCanvas
