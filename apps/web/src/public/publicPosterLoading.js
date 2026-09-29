export function preloadPublicPoster(src, { imageFactory, timeoutMs = 8000 } = {}) {
  if (typeof src !== 'string' || !src.trim()) return Promise.resolve('missing')

  const createImage = imageFactory || (() => new Image())
  return new Promise((resolve) => {
    const image = createImage()
    let settled = false
    const timeoutId = setTimeout(() => finish('timeout'), timeoutMs)
    const finish = (status) => {
      if (settled) return
      settled = true
      clearTimeout(timeoutId)
      image.onload = null
      image.onerror = null
      resolve(status)
    }
    const decode = async () => {
      try {
        if (typeof image.decode === 'function') await image.decode()
        finish('loaded')
      } catch {
        finish('error')
      }
    }

    image.onload = () => { void decode() }
    image.onerror = () => finish('error')
    image.src = src.trim()

    if (image.complete && image.naturalWidth > 0) void decode()
  })
}
