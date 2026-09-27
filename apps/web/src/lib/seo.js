const BRAND_NAME = 'Loci'
const DEFAULT_SOCIAL_IMAGE = '/branding/lockup-horizontal.svg'

function normalizeMetaContent(value) {
  if (typeof value !== 'string') {
    return ''
  }

  const trimmed = value.trim()
  return trimmed || ''
}

function upsertMeta(attribute, key, content) {
  const normalizedContent = normalizeMetaContent(content)
  const selector = `meta[${attribute}="${key}"]`
  let node = document.head.querySelector(selector)

  if (!normalizedContent) {
    if (node) {
      node.remove()
    }
    return
  }

  if (!node) {
    node = document.createElement('meta')
    node.setAttribute(attribute, key)
    document.head.appendChild(node)
  }

  node.setAttribute('content', normalizedContent)
}

function resolveMetaUrl(value) {
  const normalizedValue = normalizeMetaContent(value)
  if (!normalizedValue) {
    return ''
  }

  try {
    return new URL(normalizedValue, window.location.origin).href
  } catch {
    return normalizedValue
  }
}

export function formatLociTitle(title = '') {
  const normalizedTitle = normalizeMetaContent(title)
  if (!normalizedTitle) {
    return BRAND_NAME
  }

  if (normalizedTitle === BRAND_NAME || normalizedTitle.startsWith(`${BRAND_NAME} · `)) {
    return normalizedTitle
  }

  return `${BRAND_NAME} · ${normalizedTitle}`
}

export function setPageMeta({ title = '', description = '', image = '', url = '', updateDocumentTitle = false } = {}) {
  if (typeof document === 'undefined') {
    return
  }

  const normalizedTitle = formatLociTitle(title)
  const normalizedDescription = normalizeMetaContent(description)
  const normalizedImage = resolveMetaUrl(image || DEFAULT_SOCIAL_IMAGE)
  const normalizedUrl = resolveMetaUrl(url) || window.location.href

  if (updateDocumentTitle && normalizedTitle) {
    document.title = normalizedTitle
  }

  upsertMeta('name', 'description', normalizedDescription)
  upsertMeta('property', 'og:site_name', BRAND_NAME)
  upsertMeta('property', 'og:type', 'website')
  upsertMeta('property', 'og:title', normalizedTitle)
  upsertMeta('property', 'og:description', normalizedDescription)
  upsertMeta('property', 'og:url', normalizedUrl)
  upsertMeta('property', 'og:image', normalizedImage)
  upsertMeta('property', 'og:image:alt', normalizedImage ? `${BRAND_NAME} social card` : '')
  upsertMeta('name', 'twitter:card', normalizedImage ? 'summary_large_image' : 'summary')
  upsertMeta('name', 'twitter:title', normalizedTitle)
  upsertMeta('name', 'twitter:description', normalizedDescription)
  upsertMeta('name', 'twitter:image', normalizedImage)
}