import test from 'node:test'
import assert from 'node:assert/strict'
import { createPublicationIntentStorage, createPublicationRequestIntent, readPublicationStatus } from './publicationRequestIntent.js'

function fixture(actorId = 'actor-a', apiScope = 'https://example.invalid/api') {
  const values = new Map()
  const storage = { getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
  const persistence = createPublicationIntentStorage({ storage, actorId, apiScope })
  const intent = () => createPublicationRequestIntent({ makeKey: () => 'request-a', persistence })
  return { storage, values, persistence, intent }
}
const queued = (operation_id = 'operation-a') => ({ operation_id, state: 'QUEUED', destination: 'synthetic-local' })

// These tests cover client recovery only. The backend must authorize every read
// and supply durable admission/replay before advertising this capability.
test('storage contains only opaque request and operation metadata after durable acknowledgement', () => {
  const { values, persistence, intent } = fixture()
  const current = intent()
  current.begin()
  assert.equal(values.size, 0, 'unacknowledged or legacy requests are not falsely marked durable')
  current.accepted({ ...queued(), token: 'never-store', detail: 'private content', receipt: { secret: 'never-store' } })
  assert.deepEqual(JSON.parse(values.get(persistence.storageKey)), { version: 1, requestKey: 'request-a', operationId: 'operation-a' })
})

test('reload or a new tab recovers exact acknowledged operation without sending any POST', async () => {
  const f = fixture()
  const first = f.intent()
  first.begin(); first.accepted(queued())
  const reloaded = f.intent()
  assert.equal(reloaded.hasRecoveredOperation(), true)
  const reads = []
  await readPublicationStatus(reloaded, async (options) => { reads.push(options); return queued() })
  assert.deepEqual(reads, [{ operationId: 'operation-a' }])
  assert.equal(reloaded.begin(), 'request-a')
})

test('legacy acknowledgements do not become persistable or exact-operation reads', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted({ operation_id: 'legacy-a', state: 'RUNNING' })
  assert.equal(f.values.size, 0)
  assert.equal(f.intent().hasRecoveredOperation(), false)
  assert.equal(current.statusOperationId(), null)
})

test('an explicit real durable API capability uses the same exact-operation contract', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted({ operation_id: 'real-a', state: 'QUEUED', publication_capabilities: { durable_requests: true } })
  assert.equal(f.intent().statusOperationId(), 'real-a')
})

for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'SUPERSEDED']) {
  test(`matching ${state} clears only this request recovery record`, () => {
    const f = fixture()
    const current = f.intent()
    current.begin(); current.accepted(queued())
    current.observe({ ...queued(), state })
    assert.equal(f.values.size, 0)
    assert.equal(f.intent().hasRecoveredOperation(), false)
  })
}

test('uncertain and mismatched results retain recoverable identity', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted(queued())
  current.observe({ ...queued(), state: 'NEEDS_RECONCILIATION' })
  current.observe({ ...queued('different-operation'), state: 'SUCCEEDED' })
  assert.equal(f.intent().statusOperationId(), 'operation-a')
})

test('actor and API scopes cannot recover each other even in the same browser storage', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted(queued())
  for (const scope of [{ actorId: 'actor-b', apiScope: 'https://example.invalid/api' },
    { actorId: 'actor-a', apiScope: 'https://other.invalid/api' }]) {
    const persistence = createPublicationIntentStorage({ storage: f.storage, ...scope })
    assert.equal(createPublicationRequestIntent({ persistence }).hasRecoveredOperation(), false)
  }
})

test('malformed, extra-field, oversized and invalid identifier storage is ignored', () => {
  const f = fixture()
  for (const raw of ['null', '[]', '{', 'x'.repeat(2049),
    JSON.stringify({ version: 1, requestKey: 'ok', operationId: 'op', token: 'bad' }),
    JSON.stringify({ version: 1, requestKey: 'bad\nkey', operationId: 'op' }),
    JSON.stringify({ version: 3, requestKey: 'ok', operationId: 'op' }),
    JSON.stringify({ version: 1, requestKey: 'ok', operationId: '' })]) {
    f.values.set(f.persistence.storageKey, raw)
    assert.equal(f.intent().hasRecoveredOperation(), false)
  }
})

test('denied browser storage leaves memory-only request behavior usable', () => {
  const storage = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') }, removeItem() { throw new Error('denied') } }
  const persistence = createPublicationIntentStorage({ storage, actorId: 'actor-a', apiScope: 'https://example.invalid/api' })
  const intent = createPublicationRequestIntent({ persistence, makeKey: () => 'request-a' })
  intent.begin(); intent.accepted(queued())
  assert.equal(intent.statusOperationId(), 'operation-a')
  assert.doesNotThrow(() => intent.reset())
})

test('reset removes own recovery metadata without erasing another tab newer request', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted(queued())
  f.values.set(f.persistence.storageKey, JSON.stringify({ version: 1, requestKey: 'request-b', operationId: 'operation-b' }))
  current.reset()
  assert.equal(f.intent().statusOperationId(), 'operation-b')
})

test('late acknowledgement cannot overwrite another tab newer recovery metadata', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted(queued())
  f.values.set(f.persistence.storageKey, JSON.stringify({ version: 1, requestKey: 'request-b', operationId: 'operation-b' }))
  current.accepted({ ...queued(), state: 'RUNNING' })
  assert.equal(f.intent().statusOperationId(), 'operation-b')
})

test('storage-event restore invalidates earlier status tickets without any publication call', async () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.accepted(queued())
  let finish
  const pending = readPublicationStatus(current, () => new Promise((resolve) => { finish = resolve }))
  f.values.set(f.persistence.storageKey, JSON.stringify({ version: 1, requestKey: 'request-b', operationId: 'operation-b' }))
  assert.equal(current.restore(), true)
  finish({ ...queued(), state: 'SUCCEEDED' })
  assert.equal(await pending, null)
  assert.equal(current.statusOperationId(), 'operation-b')
})

test('storage event cannot take ownership away from an in-flight explicit POST', () => {
  const f = fixture()
  const current = f.intent()
  current.begin(); current.startPost()
  f.values.set(f.persistence.storageKey, JSON.stringify({ version: 1, requestKey: 'request-b', operationId: 'operation-b' }))
  assert.equal(current.restore(), false)
  assert.equal(current.requestCurrent('request-a'), true)
  current.finishPost()
})

test('missing or untrusted actor scope is rejected before storage access', () => {
  for (const actorId of ['', undefined, 'x\ny']) {
    assert.throws(() => createPublicationIntentStorage({ storage: {}, actorId, apiScope: 'https://example.invalid/api' }), /verified publication storage scope/)
  }
})
