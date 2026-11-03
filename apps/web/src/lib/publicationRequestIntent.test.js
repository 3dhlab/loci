import test from 'node:test'
import assert from 'node:assert/strict'
import { createPublicationRequestIntent, readPublicationStatus, sendPublicationRequest } from './publicationRequestIntent.js'
import { getLiveDatabaseSyncStatus, startLiveDatabaseSync } from './api.js'

function fixture() {
  let generated = 0
  return createPublicationRequestIntent({ makeKey: () => `request-${++generated}` })
}

function status(state, operation_id = 'operation-1', extra = {}) {
  return { state, operation_id, destination: 'synthetic-local', ...extra }
}

test('unacknowledged retries retain one request identity', () => {
  const intent = fixture()
  assert.equal(intent.begin(), 'request-1')
  assert.equal(intent.begin(), 'request-1')
  assert.equal(intent.statusOperationId(), null)
})

test('accepted queued/running requests retain identity and exact status target', () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('QUEUED'))
  assert.equal(intent.statusOperationId(), 'operation-1')
  assert.equal(intent.observe(status('RUNNING')), false)
  assert.equal(intent.begin(), 'request-1')
})

for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'SUPERSEDED']) {
  test(`only matching ${state} completion enables a new intent`, () => {
    const intent = fixture()
    intent.begin()
    intent.accepted(status('QUEUED'))
    assert.equal(intent.observe(status(state, 'some-other-operation')), false)
    assert.equal(intent.begin(), 'request-1')
    assert.equal(intent.observe(status(state)), true)
    assert.equal(intent.statusOperationId(), null)
    assert.equal(intent.begin(), 'request-2')
  })
}

test('uncertain publication remains the same intent until reconciliation', () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('QUEUED'))
  assert.equal(intent.observe(status('NEEDS_RECONCILIATION')), false)
  assert.equal(intent.begin(), 'request-1')
  assert.equal(intent.statusOperationId(), 'operation-1')
})

test('missing or contradictory acknowledgement cannot silently replace the intent', () => {
  const intent = fixture()
  intent.begin()
  assert.throws(() => intent.accepted({}), /not acknowledged/)
  assert.equal(intent.begin(), 'request-1')
  intent.accepted(status('QUEUED'))
  assert.throws(() => intent.accepted(status('QUEUED', 'other')), /changed unexpectedly/)
  assert.equal(intent.statusOperationId(), 'operation-1')
  assert.equal(intent.begin(), 'request-1')
})

test('legacy status uses the existing latest-status route', () => {
  const intent = fixture()
  intent.begin()
  intent.accepted({ operation_id: 'legacy-operation', state: 'RUNNING' })
  assert.equal(intent.statusOperationId(), null)
  assert.equal(intent.observe({ operation_id: 'legacy-operation', state: 'SUCCEEDED' }), true)
  assert.equal(intent.begin(), 'request-2')
})

test('invalid request key is rejected before any request can be sent', () => {
  const intent = createPublicationRequestIntent({ makeKey: () => 'bad\nkey' })
  assert.throws(() => intent.begin(), /valid publication request identity/)
})

function response(code, payload) {
  return { status: code, ok: code >= 200 && code < 300, headers: { get: () => null }, json: async () => payload }
}

test('actual start request preserves header on explicit retry without automatic POST retry', async () => {
  const fetchBefore = globalThis.fetch
  const calls = []
  const intent = fixture()
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    if (calls.length === 1) throw new Error('simulated lost acceptance')
    return response(202, status('QUEUED'))
  }
  try {
    await assert.rejects(startLiveDatabaseSync('synthetic-test-token', { idempotencyKey: intent.begin() }), /Could not reach/)
    assert.equal(calls.length, 1)
    intent.accepted(await startLiveDatabaseSync('synthetic-test-token', { idempotencyKey: intent.begin() }))
    assert.equal(calls.length, 2)
    assert.ok(calls.every(({ options }) => options.method === 'POST' && options.headers['Idempotency-Key'] === 'request-1'))
    assert.equal(intent.statusOperationId(), 'operation-1')
  } finally {
    globalThis.fetch = fetchBefore
  }
})

test('actual status request selects exact operation route and encodes path input', async () => {
  const fetchBefore = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return response(200, status('RUNNING'))
  }
  try {
    await getLiveDatabaseSyncStatus('synthetic-test-token', { operationId: 'op/with space' })
    await getLiveDatabaseSyncStatus('synthetic-test-token')
    assert.ok(calls[0].url.endsWith('/operations/op%2Fwith%20space'))
    assert.ok(calls[1].url.endsWith('/sync/status'))
    assert.ok(calls.every(({ options }) => options.method === 'GET'))
  } finally {
    globalThis.fetch = fetchBefore
  }
})

test('connected request action retries same intent and creates new only after terminal result', async () => {
  const keys = []
  const intent = fixture()
  const send = async ({ idempotencyKey }) => {
    keys.push(idempotencyKey)
    if (keys.length === 1) throw new Error('simulated lost response')
    return status('SUCCEEDED')
  }
  await assert.rejects(sendPublicationRequest(intent, send), /lost response/)
  await sendPublicationRequest(intent, send)
  await sendPublicationRequest(intent, send)
  assert.deepEqual(keys, ['request-1', 'request-1', 'request-2'])
})

test('connected request action ignores stale acknowledgement after a different login session', async () => {
  let resolveResponse
  const intent = fixture()
  let current = true
  const pending = sendPublicationRequest(intent, () => new Promise((resolve) => { resolveResponse = resolve }), {
    isCurrent: () => current
  })
  current = false
  resolveResponse(status('QUEUED'))
  assert.equal(await pending, null)
  assert.equal(intent.statusOperationId(), null)
})

test('stale session is refused before creating a request identity or sending a POST', async () => {
  let sent = 0
  const intent = fixture()
  assert.equal(await sendPublicationRequest(intent, () => { sent += 1 }, { isCurrent: () => false }), null)
  assert.equal(sent, 0)
  assert.equal(intent.begin(), 'request-1')
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('connected status action discards late A completion after B starts in the same session', async () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('RUNNING', 'A'))
  const earlier = deferred()
  const later = deferred()
  const displayed = []
  const onStatus = (payload) => displayed.push(`${payload.operation_id}:${payload.state}`)
  const first = readPublicationStatus(intent, () => earlier.promise, { onStatus })
  const second = readPublicationStatus(intent, () => later.promise, { onStatus })
  later.resolve(status('SUCCEEDED', 'A'))
  await second
  const b = await sendPublicationRequest(intent, async () => status('QUEUED', 'B'))
  onStatus(b)
  earlier.resolve(status('SUCCEEDED', 'A'))
  assert.equal(await first, null)
  assert.deepEqual(displayed, ['A:SUCCEEDED', 'B:QUEUED'])
  assert.equal(intent.statusOperationId(), 'B')
})

test('connected status action discards pre-start latest read after B acknowledgement', async () => {
  const intent = fixture()
  const initial = deferred()
  let displayed = status('QUEUED', 'B')
  const pending = readPublicationStatus(intent, () => initial.promise, { onStatus: (payload) => { displayed = payload } })
  await sendPublicationRequest(intent, async () => status('QUEUED', 'B'))
  initial.resolve(status('SUCCEEDED', 'A'))
  assert.equal(await pending, null)
  assert.equal(displayed.operation_id, 'B')
  assert.equal(displayed.state, 'QUEUED')
})

test('connected status action never regresses terminal completion to late running status', async () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('RUNNING', 'A'))
  const older = deferred()
  const displayed = []
  const onStatus = (payload) => displayed.push(payload.state)
  const pending = readPublicationStatus(intent, () => older.promise, { onStatus })
  await readPublicationStatus(intent, async () => status('SUCCEEDED', 'A'), { onStatus })
  older.resolve(status('RUNNING', 'A'))
  assert.equal(await pending, null)
  assert.deepEqual(displayed, ['SUCCEEDED'])
})

test('connected status action rejects a mismatched operation without updating UI', async () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('QUEUED', 'B'))
  let applied = 0
  assert.equal(await readPublicationStatus(intent, async () => status('SUCCEEDED', 'A'), {
    onStatus: () => { applied += 1 }, onCompleted: () => { applied += 1 }
  }), null)
  assert.equal(applied, 0)
  assert.equal(intent.statusOperationId(), 'B')
})

test('connected status action rejects an older same-operation response after a newer response', async () => {
  const intent = fixture()
  intent.begin()
  intent.accepted(status('QUEUED', 'A'))
  const older = deferred()
  const displayed = []
  const onStatus = (payload) => displayed.push(payload.state)
  const pending = readPublicationStatus(intent, () => older.promise, { onStatus })
  await readPublicationStatus(intent, async () => status('RUNNING', 'A'), { onStatus })
  older.resolve(status('QUEUED', 'A'))
  assert.equal(await pending, null)
  assert.deepEqual(displayed, ['RUNNING'])
})

test('connected status action ignores old-session responses and errors', async () => {
  const intent = fixture()
  let current = true
  const read = deferred()
  let applied = 0
  const pending = readPublicationStatus(intent, () => read.promise, {
    isCurrent: () => current, onStatus: () => { applied += 1 }
  })
  current = false
  read.reject(new Error('old session failure'))
  assert.equal(await pending, null)
  assert.equal(applied, 0)
})

test('late status GET cannot clear the current POST busy state', async () => {
  const intent = fixture()
  const get = deferred()
  const post = deferred()
  let busy = false
  let applied = 0
  const statusRead = readPublicationStatus(intent, () => get.promise, { onStatus: () => { applied += 1 } })
  const submit = sendPublicationRequest(intent, () => post.promise, { onBusy: (value) => { busy = value } })
  assert.equal(busy, true)
  get.resolve({ state: 'IDLE' })
  assert.equal(await statusRead, null)
  assert.equal(applied, 0)
  assert.equal(busy, true)
  post.resolve(status('QUEUED', 'B'))
  await submit
  assert.equal(busy, false)
})

test('overlapping POST requests keep busy until all current-session requests complete', async () => {
  const intent = fixture()
  const one = deferred()
  const two = deferred()
  let busy = false
  const onBusy = (value) => { busy = value }
  const first = sendPublicationRequest(intent, () => one.promise, { onBusy })
  const second = sendPublicationRequest(intent, () => two.promise, { onBusy })
  one.resolve(status('QUEUED', 'A'))
  await first
  assert.equal(busy, true)
  two.resolve(status('RUNNING', 'A'))
  await second
  assert.equal(busy, false)
})

test('late old-intent POST error cannot mark B unconfirmed or clear B busy', async () => {
  const intent = fixture()
  const first = deferred()
  const oldReplay = deferred()
  const b = deferred()
  let busy = false
  const onBusy = (value) => { busy = value }
  const one = sendPublicationRequest(intent, () => first.promise, { onBusy })
  const two = sendPublicationRequest(intent, () => oldReplay.promise, { onBusy })
  first.resolve(status('SUCCEEDED', 'A'))
  await one
  const current = sendPublicationRequest(intent, () => b.promise, { onBusy })
  oldReplay.reject(new Error('stale A request error'))
  assert.equal(await two, null)
  assert.equal(busy, true)
  b.resolve(status('QUEUED', 'B'))
  assert.equal((await current).operation_id, 'B')
  assert.equal(busy, false)
})

test('late old-intent POST acknowledgement cannot replace B or clear B busy', async () => {
  const intent = fixture()
  const first = deferred()
  const replay = deferred()
  const b = deferred()
  let busy = false
  const onBusy = (value) => { busy = value }
  const one = sendPublicationRequest(intent, () => first.promise, { onBusy })
  const two = sendPublicationRequest(intent, () => replay.promise, { onBusy })
  first.resolve(status('SUCCEEDED', 'A'))
  await one
  const current = sendPublicationRequest(intent, () => b.promise, { onBusy })
  replay.resolve(status('SUCCEEDED', 'A'))
  assert.equal(await two, null)
  assert.equal(busy, true)
  b.resolve(status('QUEUED', 'B'))
  assert.equal((await current).operation_id, 'B')
  assert.equal(intent.statusOperationId(), 'B')
  assert.equal(busy, false)
})
