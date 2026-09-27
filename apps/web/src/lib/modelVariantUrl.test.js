import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ALLOWED_MODEL_VARIANTS,
  ALLOWED_MOBILE_VARIANTS,
  MOBILE_VARIANT_DEFAULT,
  MOBILE_VARIANT_HIGH_RISK,
  OPTIMIZED_MODEL_DELIVERY_ENABLED,
  REQUIRED_VARIANT_QUERY_KEY,
  buildModelRequestUrl,
  isRequiredModelVariant,
  normalizeModelDeliveryCapabilities,
  parseOptimizedModelDeliveryCapability,
  selectMobileVariant,
  selectModelVariant,
  withModelVariant,
} from './modelVariantUrl.js'

const CANONICAL = '/api/v1/public/objects/abc/model/file?v=1-2026'
const DELIVERY = Object.freeze({
  exact_required: true,
  tiers: Object.freeze([
    Object.freeze({ client_category: 'standard', variant: 'web-8192' }),
    Object.freeze({ client_category: 'constrained', variant: 'mobile-4096' }),
  ]),
})

test('build capability remains strict and default-off', () => {
  assert.equal(OPTIMIZED_MODEL_DELIVERY_ENABLED, false)
  for (const enabled of ['1', 'true', ' TRUE ', 'enabled']) {
    assert.equal(parseOptimizedModelDeliveryCapability(enabled), true)
  }
  for (const disabled of [undefined, '', '0', 'false', 'yes']) {
    assert.equal(parseOptimizedModelDeliveryCapability(disabled), false)
  }
})

test('delivery capabilities accept public API and embed casing', () => {
  assert.deepEqual(normalizeModelDeliveryCapabilities(DELIVERY), {
    standard: 'web-8192', constrained: 'mobile-4096',
  })
  assert.deepEqual(normalizeModelDeliveryCapabilities({
    exactRequired: true,
    tiers: [
      { clientCategory: 'standard', variant: 'web-8192' },
      { clientCategory: 'constrained', variant: 'mobile-4096' },
    ],
  }), { standard: 'web-8192', constrained: 'mobile-4096' })
})

test('malformed, duplicate, incomplete, and unknown capabilities are rejected', () => {
  for (const value of [
    null,
    {},
    { exact_required: false, tiers: DELIVERY.tiers },
    { exact_required: true, tiers: [DELIVERY.tiers[0]] },
    { exact_required: true, tiers: [DELIVERY.tiers[0], DELIVERY.tiers[0]] },
    { exact_required: true, tiers: [DELIVERY.tiers[0], { client_category: 'constrained', variant: '../../x' }] },
    { exact_required: true, tiers: [DELIVERY.tiers[0], { client_category: 'constrained', variant: 'web-8192' }] },
  ]) {
    assert.equal(normalizeModelDeliveryCapabilities(value), null)
  }
})

test('enabled clients select solely from server-advertised fixed tiers', () => {
  assert.equal(selectModelVariant({ optimizedDeliveryEnabled: true, deliveryCapabilities: DELIVERY }), 'web-8192')
  assert.equal(selectModelVariant({ gated: true, optimizedDeliveryEnabled: true, deliveryCapabilities: DELIVERY }), 'mobile-4096')
  assert.equal(isRequiredModelVariant({ optimizedDeliveryEnabled: true, deliveryCapabilities: DELIVERY }, 'web-8192'), true)
  assert.equal(isRequiredModelVariant({ gated: true, optimizedDeliveryEnabled: true, deliveryCapabilities: DELIVERY }, 'mobile-4096'), true)
})

test('object identifiers do not influence optimized selection', () => {
  for (const objectId of ['', 'any-new-object', '../../another-object']) {
    assert.equal(selectModelVariant({ objectId, optimizedDeliveryEnabled: true, deliveryCapabilities: DELIVERY }), 'web-8192')
  }
})

test('default-off and absent profiles preserve established legacy behavior', () => {
  assert.equal(selectModelVariant({ deliveryCapabilities: DELIVERY }), null)
  assert.equal(selectModelVariant({ gated: true, deliveryCapabilities: DELIVERY }), MOBILE_VARIANT_DEFAULT)
  assert.equal(selectModelVariant({ optimizedDeliveryEnabled: true }), null)
  assert.equal(selectModelVariant({ gated: true, optimizedDeliveryEnabled: true }), MOBILE_VARIANT_DEFAULT)
  assert.equal(isRequiredModelVariant({ deliveryCapabilities: DELIVERY }, 'web-8192'), false)
})

test('legacy constrained selection keeps fixed safe tiers', () => {
  assert.equal(selectMobileVariant({ gated: false }), null)
  assert.equal(selectMobileVariant({ gated: true }), 'mobile-2048')
  assert.equal(selectMobileVariant({ gated: true, highRisk: true }), 'mobile-4096')
  assert.equal(MOBILE_VARIANT_HIGH_RISK, 'mobile-4096')
})

test('required request URL contains only the advertised allowlisted token', () => {
  const out = buildModelRequestUrl(CANONICAL, {
    gated: true,
    optimizedDeliveryEnabled: true,
    deliveryCapabilities: DELIVERY,
  })
  const url = new URL(out, 'http://localhost')
  assert.equal(url.searchParams.get('variant'), 'mobile-4096')
  assert.equal(url.searchParams.get(REQUIRED_VARIANT_QUERY_KEY), '1')
  assert.equal(url.searchParams.get('v'), '1-2026')
})

test('ordinary legacy requests clear stale exact-tier flags', () => {
  const out = withModelVariant(`${CANONICAL}&variant_required=1`, 'mobile-2048')
  const url = new URL(out, 'http://localhost')
  assert.equal(url.searchParams.get('variant'), 'mobile-2048')
  assert.equal(url.searchParams.has(REQUIRED_VARIANT_QUERY_KEY), false)
})

test('unknown or path-shaped tokens never reach a URL', () => {
  for (const token of ['ios-low-memory', 'mobile-9999', '../../etc/passwd', 'mobile-2048;rm']) {
    assert.equal(withModelVariant(CANONICAL, token, { required: true }), CANONICAL)
  }
})

test('global token allowlists stay fixed and path-free', () => {
  assert.deepEqual([...ALLOWED_MOBILE_VARIANTS], ['mobile-1024', 'mobile-2048', 'mobile-4096'])
  assert.deepEqual([...ALLOWED_MODEL_VARIANTS], ['mobile-1024', 'mobile-2048', 'mobile-4096', 'web-8192'])
  for (const token of ALLOWED_MODEL_VARIANTS) {
    assert.match(token, /^(?:mobile|web)-\d+$/)
  }
})
