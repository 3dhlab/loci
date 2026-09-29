import assert from 'node:assert/strict'
import test from 'node:test'

import { preloadPublicPoster } from './publicPosterLoading.js'

function fakeImage({ decode = async () => {}, fail = false } = {}) {
  return {
    complete: false,
    naturalWidth: 0,
    decode,
    src: '',
    onload: null,
    onerror: null,
    start() {
      if (fail) this.onerror?.()
      else {
        this.complete = true
        this.naturalWidth = 1
        this.onload?.()
      }
    },
  }
}

test('poster preload waits for image decode before revealing it', async () => {
  let finishDecode
  const image = fakeImage({ decode: () => new Promise((resolve) => { finishDecode = resolve }) })
  const result = preloadPublicPoster('/posters/synthetic.png', { imageFactory: () => image })
  image.start()
  let settled = false
  result.then(() => { settled = true })
  await Promise.resolve()
  assert.equal(settled, false)
  finishDecode()
  assert.equal(await result, 'loaded')
})

test('poster preload reports image errors and missing URLs', async () => {
  const image = fakeImage({ fail: true })
  const result = preloadPublicPoster('/bad.png', { imageFactory: () => {
    queueMicrotask(() => image.start())
    return image
  } })
  assert.equal(await result, 'error')
  assert.equal(await preloadPublicPoster('  '), 'missing')
})

test('poster preload times out when an image never loads', async () => {
  const image = fakeImage()
  assert.equal(await preloadPublicPoster('/slow.png', { imageFactory: () => image, timeoutMs: 5 }), 'timeout')
})
