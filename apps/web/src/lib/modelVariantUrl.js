/* Mobile 3D variant request URL — one source of truth for evidence + embed.
 *
 * The adaptive load policy (modelLoadPolicy.js) decides *whether* to gate; this
 * module decides *which* optimized variant a gated (mobile / iOS / low-memory /
 * Data Saver / constrained-iframe) client requests after the explicit "Load 3D
 * model" tap, and appends it to the EXISTING canonical model URL as a `?variant=`
 * query.
 *
 * Hard rules:
 * - Desktop/laptop normally requests the canonical model with no variant param.
 * - Only allowlisted tokens (below) are ever emitted; the poisoned legacy
 *   low-memory token from the regression is not in the allowlist and so can
 *   never be produced. The source intentionally does not contain that literal.
 * - The variant is chosen by client device policy, never user input, and is a
 *   fixed token — never a path — so this adds no user-controlled path surface.
 * - The dark/default-off backend gate is authoritative. Object-specific delivery
 *   tiers carry `variant_required=1`, which asks the API for that exact approved
 *   representation. A missing or revoked required tier produces a recoverable
 *   viewer error and can never expose the much larger canonical representation.
 * - The public web build has an independent default-off capability switch. R2
 *   resilience can ship with established model selection while R5 remains
 *   dormant. R5 activates only in a reviewed build created with
 *   `VITE_OPTIMIZED_MODEL_DELIVERY_ENABLED=1` after its backend, schema, rows,
 *   artifacts, and server kill switch are ready.
 */

// Aggressive-safe default (decoded texture RAM ~16 MB) for ordinary gated loads.
// High-risk objects (the documented iOS crasher, Synthetic Large Model) request the
// high-fidelity 4096 tier: lower texture resolution (1024/2048) was rejected by
// eye on a physical iPhone, while 4096 was accepted on device on 2026-06-30 and
// stays well under the ~1 GB-decoded canonical that crashed Safari (~64-85 MB
// decoded). Mirrors the backend ALLOWED_VARIANT_KEYS allowlist of safe tiers.
export const MOBILE_VARIANT_DEFAULT = 'mobile-2048'
export const MOBILE_VARIANT_HIGH_RISK = 'mobile-4096'
export const ALLOWED_MOBILE_VARIANTS = Object.freeze(['mobile-1024', 'mobile-2048', 'mobile-4096'])
export const ALLOWED_MODEL_VARIANTS = Object.freeze([...ALLOWED_MOBILE_VARIANTS, 'web-8192'])

export function parseOptimizedModelDeliveryCapability(value) {
  return ['1', 'true', 'enabled'].includes(String(value ?? '').trim().toLowerCase())
}

export const OPTIMIZED_MODEL_DELIVERY_ENABLED = parseOptimizedModelDeliveryCapability(
  import.meta.env?.VITE_OPTIMIZED_MODEL_DELIVERY_ENABLED
)

export const REQUIRED_VARIANT_QUERY_KEY = 'variant_required'

export function normalizeModelDeliveryCapabilities(value) {
  if (!value || typeof value !== 'object') {
    return null
  }
  const exactRequired = value.exact_required === true || value.exactRequired === true
  if (!exactRequired || !Array.isArray(value.tiers)) {
    return null
  }
  const tiers = {}
  for (const entry of value.tiers) {
    const category = entry?.client_category ?? entry?.clientCategory
    const variant = entry?.variant
    if (!['standard', 'constrained'].includes(category) || !ALLOWED_MODEL_VARIANTS.includes(variant)) {
      return null
    }
    if (tiers[category]) {
      return null
    }
    tiers[category] = variant
  }
  if (!tiers.standard || !tiers.constrained || tiers.standard === tiers.constrained) {
    return null
  }
  return Object.freeze({ standard: tiers.standard, constrained: tiers.constrained })
}

/**
 * Pick the variant token a client should request, from the load-policy result.
 * @returns {string|null} an allowlisted token, or null for un-gated (canonical).
 */
export function selectMobileVariant({ gated = false, highRisk = false } = {}) {
  if (!gated) {
    return null
  }
  return highRisk ? MOBILE_VARIANT_HIGH_RISK : MOBILE_VARIANT_DEFAULT
}

/**
 * Select an approved delivery candidate for this object and client policy.
 *
 * An object-specific tier replaces the canonical asset on un-gated clients so the
 * very large canonical texture does not remain the Mac/laptop path once its
 * release gates pass. Gated clients only take an object-specific tier when the
 * object declares one; otherwise they keep the generic mobile tier, which is
 * always sized below the gated memory budget.
 */
export function selectModelVariant({
  gated = false,
  highRisk = false,
  optimizedDeliveryEnabled = OPTIMIZED_MODEL_DELIVERY_ENABLED,
  deliveryCapabilities = null,
} = {}) {
  const tiers = optimizedDeliveryEnabled
    ? normalizeModelDeliveryCapabilities(deliveryCapabilities)
    : null
  const candidate = gated ? tiers?.constrained : tiers?.standard
  if (ALLOWED_MODEL_VARIANTS.includes(candidate)) {
    return candidate
  }
  return selectMobileVariant({ gated, highRisk })
}

/**
 * True when the selected token is an object-specific delivery tier. These tiers
 * carry an exact-representation contract so a constrained client receives an
 * approved low-memory model or a clear recoverable failure.
 */
export function isRequiredModelVariant({
  gated = false,
  optimizedDeliveryEnabled = OPTIMIZED_MODEL_DELIVERY_ENABLED,
  deliveryCapabilities = null,
} = {}, variant = '') {
  if (!optimizedDeliveryEnabled) {
    return false
  }
  const tiers = normalizeModelDeliveryCapabilities(deliveryCapabilities)
  const requiredVariant = gated ? tiers?.constrained : tiers?.standard
  return Boolean(requiredVariant && variant === requiredVariant && ALLOWED_MODEL_VARIANTS.includes(variant))
}

/**
 * Append `?variant=<token>` to a model URL. Returns the URL unchanged when there
 * is no variant or the token is not allowlisted (defense-in-depth: an unexpected
 * token never reaches the network). Handles absolute and root-relative URLs and
 * de-duplicates an existing `variant` param.
 */
export function withModelVariant(modelUrl, variant, { required = false } = {}) {
  if (!modelUrl || !variant || !ALLOWED_MODEL_VARIANTS.includes(variant)) {
    return modelUrl
  }

  const isAbsolute = /^[a-z][a-z\d+.-]*:\/\//i.test(modelUrl)
  const base = typeof window !== 'undefined' && window.location ? window.location.origin : 'http://localhost'
  try {
    const url = new URL(modelUrl, base)
    url.searchParams.set('variant', variant)
    if (required) {
      url.searchParams.set(REQUIRED_VARIANT_QUERY_KEY, '1')
    } else {
      url.searchParams.delete(REQUIRED_VARIANT_QUERY_KEY)
    }
    return isAbsolute ? url.toString() : `${url.pathname}${url.search}${url.hash}`
  } catch {
    const separator = modelUrl.includes('?') ? '&' : '?'
    const requiredQuery = required ? `&${REQUIRED_VARIANT_QUERY_KEY}=1` : ''
    return `${modelUrl}${separator}variant=${encodeURIComponent(variant)}${requiredQuery}`
  }
}

/**
 * Convenience: resolve the model request URL for a given canonical URL + policy.
 * Un-gated -> canonical unchanged; gated -> canonical + the selected variant.
 */
export function buildModelRequestUrl(modelUrl, policy = {}) {
  const variant = selectModelVariant(policy)
  return withModelVariant(modelUrl, variant, {
    required: isRequiredModelVariant(policy, variant),
  })
}
