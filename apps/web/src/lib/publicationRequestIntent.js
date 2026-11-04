// Server-side authorization and durable replay remain authoritative. Stored
// request metadata is scoped by a server-authenticated actor; it is no credential.
const TERMINAL_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'SUPERSEDED'])

function requestKey() {
  if (typeof globalThis.crypto?.randomUUID !== 'function') {
    throw new Error('A secure publication request identity could not be created.')
  }
  return globalThis.crypto.randomUUID()
}

function validIdentifier(value) {
  return typeof value === 'string' && /^[\x21-\x7e]{1,256}$/.test(value)
}

// No token, publication content, status, or receipt is stored. Recover only an
// durable request metadata. Pending keys require a fresh server capability check.
export function createPublicationIntentStorage({ storage, actorId, apiScope }) {
  if (!validIdentifier(actorId) || typeof apiScope !== 'string' || !/^[\x21-\x7e]{1,2048}$/.test(apiScope)) {
    throw new Error('A verified publication storage scope is required.')
  }
  const storageKey = `loci.publication.intent.v1:${encodeURIComponent(apiScope)}:${encodeURIComponent(actorId)}`
  function load() {
    try {
      const raw = storage.getItem(storageKey)
      if (!raw || raw.length > 2048) return null
      const value = JSON.parse(raw)
      if (Object.keys(value).sort().join(',') !== 'operationId,requestKey,version'
          || ![1, 2].includes(value.version) || !validIdentifier(value.requestKey)
          || !(validIdentifier(value.operationId) || (value.version === 2 && value.operationId === null))) return null
      return value
    } catch { return null }
  }
  return {
    storageKey,
    load,
    save(value) {
      try {
        const existing = load()
        // A delayed acknowledgement in this tab cannot replace another tab's
        // newer operation. This is recovery metadata, not a cross-tab lock.
        if (!existing || existing.requestKey === value.requestKey) {
          const record = { version: value.version === 2 ? 2 : 1, requestKey: value.requestKey, operationId: value.operationId }
          storage.setItem(storageKey, JSON.stringify(record))
          return JSON.stringify(load()) === JSON.stringify(record)
        }
        return false
      } catch { return false }
    },
    clear(requestIdentity) {
      try {
        if (load()?.requestKey === requestIdentity) storage.removeItem(storageKey)
      } catch { /* A storage failure must not change server-authoritative state. */ }
    }
  }
}

export function publicationAdmissionPolicy({ capability, legacyEndpointNotFound = false } = {}) {
  if (legacyEndpointNotFound === true && capability === null) {
    return { legacy: true, durableRequests: false, requestLookup: false, reconciliation: false, cancellation: false }
  }
  const advertised = capability?.publication_capabilities
  if (capability?.protocol_version !== 1 || capability.destination !== 'configured-public'
      || advertised?.durable_requests !== true || advertised.request_lookup !== true) {
    throw new Error('Durable publication is unavailable on this server. Saved request metadata is retained. Retry access verification before publishing.')
  }
  return { legacy: false, durableRequests: true, requestLookup: true, reconciliation: advertised.reconciliation === true, cancellation: advertised.cancellation === true }
}

export function verifiedPublicationReceipt(payload) {
  const receipt = payload?.receipt
  const readback = receipt?.http_readback
  if (payload?.state !== 'SUCCEEDED' || !receipt || !validIdentifier(payload.operation_id)
      || receipt.operation_id !== payload.operation_id
      || !validIdentifier(payload.generation_id) || receipt.generation_id !== payload.generation_id
      || !/^[a-f0-9]{64}$/.test(payload.selection_digest || '') || receipt.selection_digest !== payload.selection_digest
      || readback?.status !== 'verified' || !Number.isSafeInteger(readback.resource_count) || readback.resource_count < 0
      || typeof readback.verified_at !== 'string' || !/(Z|\+00:00)$/.test(readback.verified_at)
      || !Number.isFinite(Date.parse(readback.verified_at))) return null
  return receipt
}

export function publicationStatusForDisplay(payload) {
  if (payload?.state === 'SUCCEEDED' && payload.destination === 'configured-public'
      && payload.publication_capabilities?.durable_requests === true && !verifiedPublicationReceipt(payload)) {
    return { ...payload, reported_state: payload.state, state: 'NEEDS_RECONCILIATION',
      detail: 'The server reported completion without a matching verified live-generation receipt. Publication is blocked until the outcome is reconciled.' }
  }
  return payload
}

export function createPublicationRequestIntent({ makeKey = requestKey, persistence = null,
  durableRequests = false, requestLookup = false } = {}) {
  let key = null
  let operationId = null
  let durable = false
  let generation = 0
  let statusSequence = 0
  let lastAppliedStatus = 0
  let pendingPosts = 0
  const canLookupRequest = durableRequests === true && requestLookup === true
  let blockedRecovery = false

  function restore() {
    if (!persistence || pendingPosts) return false
    const saved = persistence.load()
    if (!saved || (saved.requestKey === key && saved.operationId === operationId)) return false
    generation += 1
    key = saved.requestKey
    operationId = saved.operationId
    durable = true
    blockedRecovery = saved.version === 2 && !saved.operationId && !canLookupRequest
    return true
  }

  function reset() {
    if (key) persistence?.clear(key)
    generation += 1
    key = null
    operationId = null
    durable = false
    blockedRecovery = false
  }

  function observe(payload) {
    if (!key || !operationId || payload?.operation_id !== operationId) return false
    if (TERMINAL_STATES.has(payload.state)) {
      if (publicationStatusForDisplay(payload)?.state === 'NEEDS_RECONCILIATION') return false
      reset()
      return true
    }
    return false
  }

  function statusRequestCurrent(ticket) {
    return ticket?.generation === generation && ticket.expectedOperationId === operationId
      && ticket.expectedRequestKey === key && ticket.sequence >= lastAppliedStatus
      && !blockedRecovery && !(key && !operationId && !canLookupRequest)
  }

  restore()

  return {
    restore,
    hasRecoveredOperation() { return Boolean(durable && operationId) },
    hasPendingRequest() { return Boolean(key && !operationId) },
    recoveryBlocked() { return blockedRecovery },
    canLookupPending() { return Boolean(canLookupRequest && key && !operationId && !blockedRecovery) },
    begin() {
      if (!key) {
        generation += 1
        key = makeKey()
      }
      if (typeof key !== 'string' || !/^[\x21-\x7e]{1,256}$/.test(key)) {
        reset()
        throw new Error('A valid publication request identity could not be created.')
      }
      if (blockedRecovery) throw new Error('The server cannot safely recover the saved publication request. Publication is blocked.')
      if (canLookupRequest) {
        durable = true
        if (!persistence?.save({ version: 2, requestKey: key, operationId })) {
          const error = new Error('The publication request could not be saved for recovery. No publication was sent. Check browser storage and retry.')
          error.kind = 'publication-not-sent'
          throw error
        }
      }
      return key
    },
    accepted(payload) {
      if (!key || typeof payload?.operation_id !== 'string' || !payload.operation_id) {
        throw new Error('The publication request was not acknowledged. Check its status before retrying.')
      }
      if (operationId && payload.operation_id !== operationId) {
        throw new Error('The publication operation changed unexpectedly. Check its status before retrying.')
      }
      operationId = payload.operation_id
      // Exact reads require the existing synthetic contract or an explicit
      // durable capability. Legacy latest status is never persisted.
      durable = durable || payload.destination === 'synthetic-local'
        || payload.publication_capabilities?.durable_requests === true
      if (durable) persistence?.save({ version: canLookupRequest ? 2 : 1, requestKey: key, operationId })
      return observe(payload)
    },
    observe,
    statusOperationId() {
      return durable ? operationId : null
    },
    statusRequest() {
      return Object.freeze({ generation, expectedOperationId: operationId, expectedRequestKey: key,
        operationId: durable ? operationId : null,
        requestKey: canLookupRequest && key && !operationId ? key : null, sequence: ++statusSequence })
    },
    statusRequestCurrent,
    operationRequestCurrent(ticket, expectedId) {
      return ticket?.generation === generation && ticket.expectedOperationId === operationId
        && ticket.expectedRequestKey === key && !(key && !operationId)
        && (!operationId || operationId === expectedId)
    },
    requestCurrent(requestIdentity) {
      return key === requestIdentity
    },
    startPost() {
      pendingPosts += 1
    },
    finishPost() {
      pendingPosts -= 1
      return pendingPosts > 0
    },
    applyStatus(ticket, payload) {
      if (!statusRequestCurrent(ticket) || typeof payload?.state !== 'string'
          || (operationId && payload.operation_id !== operationId)) return null
      if (ticket.requestKey && !operationId) {
        if (payload.state === 'REQUEST_NOT_FOUND') {
          lastAppliedStatus = ticket.sequence
          return { payload, completed: false }
        }
        if (payload.publication_capabilities?.request_lookup !== true || !validIdentifier(payload.operation_id)) return null
        lastAppliedStatus = ticket.sequence
        return { payload, completed: this.accepted(payload) }
      }
      lastAppliedStatus = ticket.sequence
      return { payload, completed: observe(payload) }
    },
    reset
  }
}

export async function sendPublicationRequest(intent, send, { isCurrent = () => true, onBusy = () => {} } = {}) {
  if (!isCurrent()) return null
  intent.startPost()
  onBusy(true)
  let idempotencyKey
  try {
    idempotencyKey = intent.begin()
    const payload = await send({ idempotencyKey })
    if (!isCurrent() || !intent.requestCurrent(idempotencyKey)) return null
    intent.accepted(payload)
    return payload
  } catch (error) {
    if (!isCurrent() || (idempotencyKey && !intent.requestCurrent(idempotencyKey))) return null
    throw error
  } finally {
    const busy = intent.finishPost()
    if (isCurrent()) onBusy(busy)
  }
}

export async function resolvePublicationOperation(intent, resolve, {
  operationId, isCurrent = () => true, operationCurrent = () => true, onBusy = () => {}
} = {}) {
  const ticket = intent.statusRequest()
  const current = () => isCurrent() && operationCurrent() && intent.operationRequestCurrent(ticket, operationId)
  if (!validIdentifier(operationId) || !current()) return null
  intent.startPost()
  onBusy(true)
  try {
    const payload = await resolve(operationId)
    if (!current()) return null
    if (payload?.operation_id !== operationId) throw new Error('The publication operation changed while resolving the request.')
    // A resolution completes after its earlier reads: apply a fresh sequence so
    // a delayed GET cannot later replace the acknowledged terminal outcome.
    return intent.applyStatus(intent.statusRequest(), payload)?.payload ?? null
  } catch (error) {
    if (!current()) return null
    throw error
  } finally {
    const busy = intent.finishPost()
    // Session scope owns busy state, including when the operation has changed.
    if (isCurrent()) onBusy(busy)
  }
}

export async function readPublicationStatus(intent, read, {
  isCurrent = () => true, onStatus = () => {}, onCompleted = () => {}
} = {}) {
  if (!isCurrent()) return null
  const ticket = intent.statusRequest()
  if (!intent.statusRequestCurrent(ticket)) return null
  let payload
  try {
    payload = await read(ticket.requestKey ? { idempotencyKey: ticket.requestKey } : { operationId: ticket.operationId })
  } catch (error) {
    if (!isCurrent() || !intent.statusRequestCurrent(ticket)) return null
    if (ticket.requestKey && error.status === 404) {
      payload = { state: 'REQUEST_NOT_FOUND', operation_id: null, detail: 'The server has not recorded this request. You can retry with the same request identity.' }
    } else {
      throw error
    }
  }
  if (!isCurrent()) return null
  const result = intent.applyStatus(ticket, payload)
  if (!result) return null
  if (result.completed) onCompleted()
  onStatus(result.payload)
  return result.payload
}
