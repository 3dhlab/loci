import test from 'node:test'
import assert from 'node:assert/strict'
import { createPublicationIntentStorage, createPublicationRequestIntent, readPublicationStatus, resolvePublicationOperation,
  sendPublicationRequest, publicationAdmissionPolicy, publicationStatusForDisplay, verifiedPublicationReceipt } from './publicationRequestIntent.js'
import { getLiveDatabaseSyncCapabilities, getLiveDatabaseSyncStatus, reconcileLiveDatabaseSync, syncLiveDatabase, cancelLiveDatabaseSync } from './api.js'

const capabilities = { durable_requests: true, request_lookup: true, reconciliation: true, cancellation: true }
function status(state = 'QUEUED', extra = {}) {
  return { operation_id: 'operation-real', state, destination: 'configured-public', publication_capabilities: capabilities, ...extra }
}
function fixture() {
  const values = new Map()
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
  const persistence = createPublicationIntentStorage({ storage, actorId: 'actor-a', apiScope: 'https://example.invalid/api' })
  const intent = (options = {}) => createPublicationRequestIntent({ persistence, makeKey: () => 'request-real', durableRequests: true, requestLookup: true, ...options })
  return { values, storage, persistence, intent }
}
function verifiedStatus() {
  const payload = status('SUCCEEDED', { generation_id: 'generation-real', selection_digest: 'a'.repeat(64) })
  payload.receipt = { operation_id: payload.operation_id, generation_id: payload.generation_id, selection_digest: payload.selection_digest,
    http_readback: { status: 'verified', resource_count: 4, verified_at: '2026-11-03T20:00:00Z' } }
  return payload
}

// Fixture protocol tests cannot establish a durable worker or destination effect.
test('both fresh server capabilities are required before pending keys can be stored', () => {
  for (const options of [{ durableRequests: false }, { requestLookup: false }, { requestLookup: 'true' }]) {
    const f = fixture()
    const intent = f.intent(options)
    intent.begin()
    assert.equal(f.values.size, 0)
    assert.equal(intent.canLookupPending(), false)
  }
})

test('a durable request key is written and read back before the first POST can run', async () => {
  const f = fixture()
  const intent = f.intent()
  await assert.rejects(sendPublicationRequest(intent, async ({ idempotencyKey }) => {
    assert.deepEqual(f.persistence.load(), { version: 2, requestKey: idempotencyKey, operationId: null })
    throw new Error('lost acknowledgement')
  }), /lost acknowledgement/)
  assert.equal(f.intent().canLookupPending(), true)
})

test('lost acknowledgement reload recovers through authorized key lookup without a second POST', async () => {
  const f = fixture()
  let posts = 0
  await assert.rejects(sendPublicationRequest(f.intent(), async () => { posts += 1; throw new Error('lost acknowledgement') }))
  const reloaded = f.intent()
  const reads = []
  await readPublicationStatus(reloaded, async (options) => { reads.push(options); return status() })
  assert.deepEqual(reads, [{ idempotencyKey: 'request-real' }])
  assert.equal(reloaded.statusOperationId(), 'operation-real')
  assert.equal(f.persistence.load().operationId, 'operation-real')
  assert.equal(posts, 1)
})

test('a by-key 404 retains the same key for an explicit manual retry', async () => {
  const f = fixture()
  const intent = f.intent()
  intent.begin()
  const payload = await readPublicationStatus(intent, async () => { const error = new Error('not found'); error.status = 404; throw error })
  assert.equal(payload.state, 'REQUEST_NOT_FOUND')
  assert.equal(f.persistence.load().requestKey, 'request-real')
  const retried = []
  await sendPublicationRequest(f.intent(), async (options) => { retried.push(options); return status() })
  assert.deepEqual(retried, [{ idempotencyKey: 'request-real' }])
})

test('an unknown pending request is never adopted from a legacy latest-status payload', async () => {
  const f = fixture()
  const intent = f.intent()
  intent.begin()
  assert.equal(await readPublicationStatus(intent, async () => ({ operation_id: 'unrelated', state: 'RUNNING' })), null)
  assert.equal(intent.statusOperationId(), null)
  assert.equal(f.persistence.load().operationId, null)
})

test('pending metadata on an unsupported server blocks reads and new publication', async () => {
  const f = fixture()
  f.intent().begin()
  const unsupported = f.intent({ requestLookup: false })
  assert.equal(unsupported.recoveryBlocked(), true)
  let requests = 0
  assert.equal(await readPublicationStatus(unsupported, async () => { requests += 1 }), null)
  await assert.rejects(sendPublicationRequest(unsupported, async () => { requests += 1 }), /cannot safely recover/)
  assert.equal(requests, 0)
  assert.equal(f.persistence.load().requestKey, 'request-real')
})

test('failed storage write prevents durable publication before any network mutation', async () => {
  const f = fixture()
  f.storage.setItem = () => { throw new Error('denied') }
  let posts = 0
  await assert.rejects(sendPublicationRequest(f.intent(), async () => { posts += 1 }), /No publication was sent/)
  assert.equal(posts, 0)
})

test('a storage write without matching readback also prevents POST', async () => {
  const f = fixture()
  f.storage.setItem = () => {}
  let posts = 0
  await assert.rejects(sendPublicationRequest(f.intent(), async () => { posts += 1 }), /No publication was sent/)
  assert.equal(posts, 0)
})

test('another tab newer request cannot be overwritten or followed by a conflicting POST', async () => {
  const f = fixture()
  const current = f.intent()
  f.values.set(f.persistence.storageKey, JSON.stringify({ version: 2, requestKey: 'other-request', operationId: null }))
  let posts = 0
  await assert.rejects(sendPublicationRequest(current, async () => { posts += 1 }), /No publication was sent/)
  assert.equal(posts, 0)
  assert.equal(f.persistence.load().requestKey, 'other-request')
})

test('network lookup failure keeps the pending identity and never retries a POST', async () => {
  const f = fixture()
  const current = f.intent(); current.begin()
  await assert.rejects(readPublicationStatus(current, async () => { throw new Error('offline') }), /offline/)
  assert.equal(f.intent().canLookupPending(), true)
})

test('late pre-admission status cannot overwrite a newly saved pending request', async () => {
  const f = fixture()
  const current = f.intent()
  let finish
  const pending = readPublicationStatus(current, () => new Promise((resolve) => { finish = resolve }))
  current.begin()
  finish(status('SUCCEEDED'))
  assert.equal(await pending, null)
  assert.equal(current.canLookupPending(), true)
})

test('actual key lookup uses GET and a header while keeping the raw key out of URLs', async () => {
  const before = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, status: 200, json: async () => status(), headers: { get: () => null } } }
  try {
    await getLiveDatabaseSyncStatus('generated-test-token', { idempotencyKey: 'request-real' })
    assert.equal(calls.length, 1)
    assert.ok(calls[0].url.endsWith('/sync/status'))
    assert.ok(!calls[0].url.includes('request-real'))
    assert.equal(calls[0].options.method, 'GET')
    assert.equal(calls[0].options.headers['Idempotency-Key'], 'request-real')
  } finally { globalThis.fetch = before }
})

test('actual capability read is authenticated and read-only', async () => {
  const before = globalThis.fetch
  let call
  globalThis.fetch = async (url, options) => { call = { url, options }; return { ok: true, status: 200, json: async () => ({ publication_capabilities: capabilities }), headers: { get: () => null } } }
  try {
    await getLiveDatabaseSyncCapabilities('generated-test-token')
    assert.ok(call.url.endsWith('/sync/capabilities'))
    assert.equal(call.options.method, 'GET')
    assert.equal(call.options.headers.Authorization, 'Bearer generated-test-token')
  } finally { globalThis.fetch = before }
})

test('explicit reconciliation uses its distinct endpoint with no automatic retry', async () => {
  const before = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); throw new Error('lost reconciliation response') }
  try {
    await assert.rejects(reconcileLiveDatabaseSync('generated-test-token', 'operation-real'), /Could not reach/)
    assert.equal(calls.length, 1)
    assert.ok(calls[0].url.endsWith('/sync/operation-real/reconcile'))
    assert.equal(calls[0].options.method, 'POST')
    assert.equal(calls[0].options.body, undefined)
  } finally { globalThis.fetch = before }
})

test('only a matching generation and verified readback supplies a verified live receipt', () => {
  const valid = verifiedStatus()
  assert.equal(verifiedPublicationReceipt(valid), valid.receipt)
  for (const change of [
    { state: 'RUNNING' }, { receipt: null }, { generation_id: 'wrong' }, { selection_digest: 'b'.repeat(64) },
    { receipt: { ...valid.receipt, operation_id: 'other' } },
    { receipt: { ...valid.receipt, http_readback: { ...valid.receipt.http_readback, status: 'pending' } } },
    { receipt: { ...valid.receipt, http_readback: { ...valid.receipt.http_readback, resource_count: -1 } } },
    { receipt: { ...valid.receipt, http_readback: { ...valid.receipt.http_readback, verified_at: 'not a time' } } }
  ]) assert.equal(verifiedPublicationReceipt({ ...valid, ...change }), null)
})

test('claimed success without a matching real receipt retains identity and blocks new publication in display', () => {
  const f = fixture()
  const current = f.intent(); current.begin(); current.accepted(status())
  const invalid = status('SUCCEEDED')
  assert.equal(current.observe(invalid), false)
  assert.equal(current.statusOperationId(), 'operation-real')
  assert.equal(publicationStatusForDisplay(invalid).state, 'NEEDS_RECONCILIATION')
  assert.equal(f.persistence.load().requestKey, 'request-real')
})

test('matching verified success can retire the durable request after key recovery', async () => {
  const f = fixture()
  f.intent().begin()
  const reloaded = f.intent()
  const payload = await readPublicationStatus(reloaded, async () => verifiedStatus())
  assert.equal(payload.state, 'SUCCEEDED')
  assert.equal(f.values.size, 0)
})


test('a present unavailable durable protocol cannot fall through to an unsaved legacy POST', async () => {
  for (const advertised of [
    { durable_requests: false, request_lookup: false },
    { durable_requests: true, request_lookup: false },
    { durable_requests: false, request_lookup: true }
  ]) {
    const f = fixture()
    f.intent().begin()
    const saved = f.persistence.load()
    let posts = 0
    await assert.rejects((async () => {
      const policy = publicationAdmissionPolicy({ capability: { protocol_version: 1, destination: 'configured-public', publication_capabilities: advertised } })
      await sendPublicationRequest(f.intent(policy), async () => { posts += 1 })
    })(), /Durable publication is unavailable/)
    assert.equal(posts, 0)
    assert.deepEqual(f.persistence.load(), saved, 'Existing pending metadata remains intact')
  }
})

test('only an explicitly observed capability-endpoint404 permits legacy admission policy', () => {
  assert.deepEqual(publicationAdmissionPolicy({ capability: null, legacyEndpointNotFound: true }),
    { legacy: true, durableRequests: false, requestLookup: false, reconciliation: false, cancellation: false })
  for (const value of [{ capability: null }, { capability: {} }, { capability: { legacyEndpointNotFound: true } },
    { capability: null, legacyEndpointNotFound: 'true' }]) {
    assert.throws(() => publicationAdmissionPolicy(value), /Durable publication is unavailable/)
  }
})

test('supported capability policy preserves both durability requirements and reconciliation advertisement', () => {
  assert.deepEqual(publicationAdmissionPolicy({ capability: { protocol_version: 1, destination: 'configured-public', publication_capabilities: capabilities } }),
    { legacy: false, durableRequests: true, requestLookup: true, reconciliation: true, cancellation: true })
  assert.throws(() => publicationAdmissionPolicy({ capability: { protocol_version: 2, destination: 'configured-public', publication_capabilities: capabilities } }), /unavailable/)
})


test('the compatibility sync helper requires a retained key and returns the durable202 operation', async () => {
  const before = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { status: 202, ok: true, headers: { get: () => null }, json: async () => status() } }
  try {
    await assert.rejects(syncLiveDatabase('generated-test-token'), /identity is required/)
    assert.equal(calls.length, 0)
    const payload = await syncLiveDatabase('generated-test-token', { idempotencyKey: 'request-real' })
    assert.equal(payload.operation_id, 'operation-real')
    assert.equal(payload.state, 'QUEUED')
    assert.equal(calls.length, 1)
    assert.ok(calls[0].url.endsWith('/live-database/sync'))
    assert.equal(calls[0].options.method, 'POST')
    assert.equal(calls[0].options.headers['Idempotency-Key'], 'request-real')
  } finally { globalThis.fetch = before }
})


test('explicit cancellation uses only its actor-authorized endpoint without automatic retry', async () => {
  const before = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); throw new Error('lost cancellation acknowledgement') }
  try {
    await assert.rejects(cancelLiveDatabaseSync('generated-test-token', 'operation-real'), /Could not reach/)
    assert.equal(calls.length, 1)
    assert.ok(calls[0].url.endsWith('/sync/operation-real/cancel'))
    assert.equal(calls[0].options.method, 'POST')
    assert.equal(calls[0].options.body, undefined)
  } finally { globalThis.fetch = before }
})

test('superseded publication closes the request without claiming a current verified live receipt', () => {
  const f = fixture()
  const intent = f.intent(); intent.begin(); intent.accepted(status())
  const superseded = { ...verifiedStatus(), state: 'SUPERSEDED', detail: 'Previously published; the destination now has a different head.' }
  assert.equal(intent.observe(superseded), true)
  assert.equal(f.values.size, 0)
  assert.equal(publicationStatusForDisplay(superseded).state, 'SUPERSEDED')
  assert.equal(verifiedPublicationReceipt(superseded), null)
})


function deferredResult() {
  let resolve
  const promise = new Promise((yes) => { resolve = yes })
  return { promise, resolve }
}

test('a delayed cancellation for A cannot replace a newer B operation', async () => {
  const f = fixture()
  const intent = f.intent(); intent.begin(); intent.accepted(status('NEEDS_RECONCILIATION'))
  const response = deferredResult()
  const old = resolvePublicationOperation(intent, () => response.promise, { operationId: 'operation-real' })
  intent.observe(status('CANCELLED'))
  intent.begin(); intent.accepted(status('QUEUED', { operation_id: 'operation-b' }))
  response.resolve(status('CANCELLED'))
  assert.equal(await old, null)
  assert.equal(intent.statusOperationId(), 'operation-b')
})

test('resolution of a latest operation without a local key prevents an older GET regression', async () => {
  const intent = createPublicationRequestIntent()
  const resolution = deferredResult()
  const oldRead = deferredResult()
  const resolving = resolvePublicationOperation(intent, () => resolution.promise, { operationId: 'operation-real' })
  const reading = readPublicationStatus(intent, () => oldRead.promise)
  resolution.resolve(status('CANCELLED'))
  assert.equal((await resolving).state, 'CANCELLED')
  oldRead.resolve(status('RUNNING'))
  assert.equal(await reading, null)
})

test('a stale resolution cannot clear B busy ownership or leave it permanently busy', async () => {
  const f = fixture()
  const intent = f.intent(); intent.begin(); intent.accepted(status('NEEDS_RECONCILIATION'))
  const cancel = deferredResult()
  const post = deferredResult()
  let busy = false
  const onBusy = (value) => { busy = value }
  const old = resolvePublicationOperation(intent, () => cancel.promise, { operationId: 'operation-real', onBusy })
  intent.observe(status('CANCELLED'))
  const next = sendPublicationRequest(intent, () => post.promise, { onBusy })
  cancel.resolve(status('CANCELLED'))
  assert.equal(await old, null)
  assert.equal(busy, true)
  post.resolve(status('QUEUED', { operation_id: 'operation-b' }))
  await next
  assert.equal(busy, false)
})
