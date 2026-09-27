import BrandLockup from './BrandLockup'

const ERROR_KIND_SET = new Set(['parse', 'not_found', 'unauthorized', 'network'])

const ERROR_COPY = {
  parse: {
    title: 'This link needs attention',
    message: 'The published link includes settings that Loci cannot open.'
  },
  not_found: {
    title: 'Published page not found',
    message: 'The published page you requested is no longer available.'
  },
  unauthorized: {
    title: 'Published page is protected',
    message: 'Access to this published page requires authorization.'
  },
  network: {
    title: 'Connection interrupted',
    message: 'Loci could not reach the published service right now.'
  }
}

function normalizeErrorKind(value) {
  return ERROR_KIND_SET.has(value) ? value : ''
}

function extractErrorMessage(error) {
  if (typeof error === 'string') {
    return error.trim()
  }

  if (error && typeof error === 'object' && typeof error.message === 'string') {
    return error.message.trim()
  }

  return ''
}

export function inferErrorStateKind(error, fallback = 'network') {
  const explicitKind = normalizeErrorKind(error?.kind)
  if (explicitKind) {
    return explicitKind
  }

  const status = Number(error?.status)
  if (status === 401 || status === 403) {
    return 'unauthorized'
  }
  if (status === 404) {
    return 'not_found'
  }

  const message = extractErrorMessage(error).toLowerCase()
  if (message.includes('not found') || message.includes('does not exist')) {
    return 'not_found'
  }
  if (message.includes('unauthorized') || message.includes('forbidden') || message.includes('not authenticated')) {
    return 'unauthorized'
  }
  if (message.includes('network') || message.includes('failed to fetch') || message.includes('could not reach the api')) {
    return 'network'
  }

  return normalizeErrorKind(fallback) || 'network'
}

export function buildErrorState(error, {
  kind = '',
  fallbackKind = 'network',
  parseDetail = '',
  notFoundDetail = '',
  unauthorizedDetail = '',
  networkDetail = '',
  defaultDetail = ''
} = {}) {
  const resolvedKind = normalizeErrorKind(kind) || inferErrorStateKind(error, fallbackKind)
  const rawDetail = extractErrorMessage(error)

  if (resolvedKind === 'parse') {
    return {
      kind: 'parse',
      detail: parseDetail || rawDetail || ERROR_COPY.parse.message
    }
  }

  if (resolvedKind === 'not_found') {
    return {
      kind: 'not_found',
      detail: notFoundDetail || defaultDetail || rawDetail || ERROR_COPY.not_found.message
    }
  }

  if (resolvedKind === 'unauthorized') {
    return {
      kind: 'unauthorized',
      detail: unauthorizedDetail || defaultDetail || rawDetail || ERROR_COPY.unauthorized.message
    }
  }

  return {
    kind: 'network',
    detail: networkDetail || defaultDetail || rawDetail || ERROR_COPY.network.message
  }
}

export default function ErrorState({
  kind = 'network',
  kicker = 'Loci',
  title = '',
  message = '',
  detail = '',
  className = '',
  headingAs = 'h1',
  showBranding = false,
  primaryActionLabel = '',
  onPrimaryAction = null,
  secondaryActionLabel = '',
  onSecondaryAction = null,
}) {
  const resolvedKind = normalizeErrorKind(kind) || 'network'
  const resolvedCopy = ERROR_COPY[resolvedKind]
  const resolvedTitle = title || resolvedCopy.title
  const resolvedMessage = message || resolvedCopy.message
  const resolvedDetail = typeof detail === 'string' ? detail.trim() : ''
  const HeadingTag = headingAs

  return (
    <section
      className={`error-state-card ${className}`.trim()}
      data-error-kind={resolvedKind}
      role="alert"
      aria-live="assertive"
    >
      {showBranding ? (
        <div className="login-branding">
          <BrandLockup variant="stacked" />
          <p className="brand-tagline">Evidence, located.</p>
        </div>
      ) : null}
      {kicker ? <p className="kicker">{kicker}</p> : null}
      <HeadingTag>{resolvedTitle}</HeadingTag>
      <p className="muted">{resolvedMessage}</p>
      {resolvedDetail && resolvedDetail !== resolvedMessage ? (
        <p className="error-state-detail">{resolvedDetail}</p>
      ) : null}
      {(primaryActionLabel && typeof onPrimaryAction === 'function') || (secondaryActionLabel && typeof onSecondaryAction === 'function') ? (
        <div className="error-state-actions">
          {primaryActionLabel && typeof onPrimaryAction === 'function' ? (
            <button type="button" onClick={onPrimaryAction}>{primaryActionLabel}</button>
          ) : null}
          {secondaryActionLabel && typeof onSecondaryAction === 'function' ? (
            <button type="button" className="ghost" onClick={onSecondaryAction}>{secondaryActionLabel}</button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}