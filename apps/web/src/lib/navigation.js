const LOCATION_CHANGE_EVENT = 'semantic:locationchange'
export const PUBLIC_BROWSE_PATH = '/public'

let historyPatched = false
let popstateSubscribed = false

function dispatchLocationChange() {
  if (typeof window === 'undefined') {
    return
  }

  window.dispatchEvent(new Event(LOCATION_CHANGE_EVENT))
}

export function readWindowLocation() {
  if (typeof window === 'undefined') {
    return {
      pathname: '',
      search: '',
      hash: ''
    }
  }

  return {
    pathname: window.location.pathname || '',
    search: window.location.search || '',
    hash: window.location.hash || ''
  }
}

export function currentLocationKey() {
  const { pathname, search } = readWindowLocation()
  return `${pathname}${search}`
}

export function ensureLocationChangeEvents() {
  if (typeof window === 'undefined') {
    return
  }

  if (!historyPatched) {
    const originalPushState = window.history.pushState.bind(window.history)
    const originalReplaceState = window.history.replaceState.bind(window.history)

    window.history.pushState = (...args) => {
      const result = originalPushState(...args)
      dispatchLocationChange()
      return result
    }

    window.history.replaceState = (...args) => {
      const result = originalReplaceState(...args)
      dispatchLocationChange()
      return result
    }

    historyPatched = true
  }

  if (!popstateSubscribed) {
    window.addEventListener('popstate', dispatchLocationChange)
    popstateSubscribed = true
  }
}

export function subscribeToLocationChanges(listener) {
  if (typeof window === 'undefined') {
    return () => {}
  }

  ensureLocationChangeEvents()
  window.addEventListener(LOCATION_CHANGE_EVENT, listener)

  return () => {
    window.removeEventListener(LOCATION_CHANGE_EVENT, listener)
  }
}

export function navigateToPublicBrowse(options = {}) {
  return navigateToUrl(PUBLIC_BROWSE_PATH, options)
}

export function navigateToUrl(targetUrl, { replace = false, state = null } = {}) {
  if (typeof window === 'undefined') {
    return false
  }

  const normalizedTarget = typeof targetUrl === 'string' ? targetUrl.trim() : ''
  if (!normalizedTarget) {
    return false
  }

  let url
  try {
    url = new URL(normalizedTarget, window.location.href)
  } catch {
    window.location.assign(normalizedTarget)
    return true
  }

  if (url.origin !== window.location.origin) {
    window.location.assign(url.toString())
    return true
  }

  ensureLocationChangeEvents()

  const nextLocation = `${url.pathname}${url.search}${url.hash}`
  const currentLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`
  const nextState = state && typeof state === 'object' ? state : null

  if (nextLocation === currentLocation) {
    if (nextState) {
      window.history.replaceState(nextState, '', nextLocation)
      dispatchLocationChange()
    }
    return true
  }

  const historyMethod = replace ? 'replaceState' : 'pushState'
  window.history[historyMethod](nextState, '', nextLocation)
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
  return true
}