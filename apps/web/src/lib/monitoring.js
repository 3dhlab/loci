const dsn = String(import.meta.env.VITE_SENTRY_DSN || '').trim()
const tunnel = '/api/v1/public/sentry-tunnel'
const release = String(import.meta.env.VITE_APP_RELEASE || '').trim()
const environment = String(import.meta.env.MODE || 'development').trim() || 'development'
const monitoringEnabled = Boolean(import.meta.env.PROD && dsn)

let monitoringInitialized = false
let sentryModule = null
let sentryModulePromise = null

async function loadSentryModule() {
  if (!monitoringEnabled) {
    return null
  }

  if (sentryModule) {
    return sentryModule
  }

  if (!sentryModulePromise) {
    sentryModulePromise = import('@sentry/react')
      .then((module) => {
        sentryModule = module
        return module
      })
      .catch((error) => {
        sentryModulePromise = null
        throw error
      })
  }

  return sentryModulePromise
}

export function initializeMonitoring() {
  if (!monitoringEnabled || monitoringInitialized) {
    return
  }

  monitoringInitialized = true

  void loadSentryModule()
    .then((Sentry) => {
      if (!Sentry) {
        return
      }

      Sentry.init({
        dsn,
        tunnel,
        enabled: true,
        environment,
        release: release || undefined,
        tracesSampleRate: 0,
        normalizeDepth: 6,
      })
    })
    .catch((error) => {
      monitoringInitialized = false
      console.warn('[monitoring] Sentry initialization skipped:', error?.message || String(error))
    })
}

export function captureViewError(error, { viewName = '', route = '', componentStack = '' } = {}) {
  if (!monitoringEnabled || !error) {
    return
  }

  void loadSentryModule()
    .then((Sentry) => {
      if (!Sentry) {
        return
      }

      Sentry.withScope((scope) => {
        if (viewName) {
          scope.setTag('view.name', viewName)
        }
        if (route) {
          scope.setTag('route.path', route)
        }
        if (componentStack) {
          scope.setExtra('componentStack', componentStack)
        }

        Sentry.captureException(error)
      })
    })
    .catch((loadError) => {
      console.warn('[monitoring] Sentry capture skipped:', loadError?.message || String(loadError))
    })
}

export function isMonitoringEnabled() {
  return monitoringEnabled
}