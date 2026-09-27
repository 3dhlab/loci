import test from 'node:test'
import assert from 'node:assert/strict'

import { apiRequest } from './api.js'

function response(status, payload = {}, headers = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: {
      get(name) {
        const key = Object.keys(headers).find((candidate) => candidate.toLowerCase() === name.toLowerCase())
        return key ? headers[key] : null
      },
    },
    async json() {
      return payload
    },
  }
}

async function withFetchSequence(sequence, callback) {
  const originalFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    const next = sequence.shift()
    if (next instanceof Error) {
      throw next
    }
    return next
  }
  try {
    await callback(calls)
  } finally {
    globalThis.fetch = originalFetch
  }
}

test('GET retries 429/503 at most twice and returns the final success', async () => {
  await withFetchSequence([
    response(503, {}, { 'Retry-After': '0' }),
    response(429, {}, { 'Retry-After': '0' }),
    response(200, { ok: true }),
  ], async (calls) => {
    assert.deepEqual(await apiRequest('/api/v1/public/objects'), { ok: true })
    assert.equal(calls.length, 3)
    assert.ok(calls.every((call) => call.options.method === 'GET'))
  })
})

test('GET stops after the bounded retry budget', async () => {
  await withFetchSequence([
    response(503, {}, { 'Retry-After': '0' }),
    response(503, {}, { 'Retry-After': '0' }),
    response(503, { detail: 'still unavailable' }),
  ], async (calls) => {
    await assert.rejects(
      apiRequest('/api/v1/public/objects'),
      (error) => error.status === 503 && error.kind === 'unavailable'
    )
    assert.equal(calls.length, 3)
  })
})

test('write methods never retry a transient response', async () => {
  await withFetchSequence([
    response(503, { detail: 'write unavailable' }, { 'Retry-After': '0' }),
    response(200, { unexpected: true }),
  ], async (calls) => {
    await assert.rejects(
      apiRequest('/api/v1/projects', { method: 'post', body: { title: 'Local test' } }),
      (error) => error.status === 503
    )
    assert.equal(calls.length, 1)
    assert.equal(calls[0].options.method, 'POST')
  })
})
