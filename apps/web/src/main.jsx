import React, { Suspense, useEffect, useLayoutEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import BrandLockup from './components/BrandLockup'
import BrandMotion from './components/BrandMotion'
import ViewErrorBoundary from './components/ViewErrorBoundary'
import EvidenceLoadingState from './evidence/EvidenceLoadingState'
import { captureViewError, initializeMonitoring } from './lib/monitoring'
import { readWindowLocation, subscribeToLocationChanges } from './lib/navigation'
import { isStudioSurfaceEnabled, resolveStudioPaletteSync } from './public/studioSurface'
import './styles.css'

const ConsoleApp = React.lazy(() => import('./App'))
const EmbedObjectApp = React.lazy(() => import('./embed/EmbedObjectApp'))
const EvidenceObjectApp = React.lazy(() => import('./evidence/EvidenceObjectApp'))
const PublicBrowseApp = React.lazy(() => import('./public/PublicBrowseApp'))
const StudioLandingPreview = React.lazy(() => import('./public/StudioLandingPreview'))

function isPublicStudioRoute(pathname) {
  return (
    pathname === '/' ||
    pathname === '/public' ||
    pathname.startsWith('/public/') ||
    pathname.startsWith('/embed/object/') ||
    pathname.startsWith('/evidence/objects/')
  )
}

function publicStudioEnabled(pathname, search) {
  return isPublicStudioRoute(pathname) && isStudioSurfaceEnabled(search, { defaultEnabled: true })
}

class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, errorInfo) {
    console.error('Root render error:', error)
    captureViewError(error, {
      viewName: 'Root application',
      route: typeof window !== 'undefined' ? window.location.pathname : '',
      componentStack: errorInfo?.componentStack || '',
    })
  }

  render() {
    if (this.state.error) {
      // P4.8/P4.9 — public crash fallbacks use the approved Studio palette by
      // default; `?studio=0` / `?legacy=1` keeps the legacy fallback available.
      const pathname = typeof window !== 'undefined' ? window.location.pathname : ''
      const search = typeof window !== 'undefined' ? window.location.search : ''
      const studioEnabled = publicStudioEnabled(pathname, search)
      const studioPalette = studioEnabled ? resolveStudioPaletteSync() : undefined
      const mainClassName = studioEnabled
        ? 'login-screen studio-surface studio-error-fallback'
        : 'login-screen'
      return (
        <main className={mainClassName} data-studio-theme={studioPalette}>
          <section className="login-card">
            <div className="login-branding">
              <BrandLockup variant="stacked" />
              <p className="brand-tagline">Evidence, located.</p>
            </div>
            <p className="kicker">Loci Console</p>
            <h1>Frontend runtime error</h1>
            <p className="muted">A client-side error interrupted rendering. Reload this page to try again, or return to the public browse view.</p>
            <a className="button-link" href="/public">Return to public browse</a>
          </section>
        </main>
      )
    }

    return this.props.children
  }
}

function rootAppForPath(pathname, search) {
  if (pathname === '/public' || pathname.startsWith('/public/')) {
    return PublicBrowseApp
  }

  if (pathname.startsWith('/embed/object/')) {
    return EmbedObjectApp
  }

  if (pathname.startsWith('/evidence/objects/')) {
    return EvidenceObjectApp
  }

  if (pathname === '/' && publicStudioEnabled(pathname, search)) {
    return StudioLandingPreview
  }

  return ConsoleApp
}

function RootLoadingFallback({ pathname, search }) {
  const isPublicRoute = pathname === '/public' || pathname.startsWith('/public/')
  const isEmbedRoute = pathname.startsWith('/embed/object/')
  const isEvidenceRoute = pathname.startsWith('/evidence/objects/')
  const surfaceLabel = isPublicRoute
    ? 'Loci Public'
    : isEmbedRoute
      ? 'Loci Embed'
      : isEvidenceRoute
        ? 'Loci Evidence'
        : 'Loci Console'
  const heading = isPublicRoute
    ? 'Loading published objects'
    : isEmbedRoute
      ? 'Loading embed viewer'
      : isEvidenceRoute
        ? 'Loading evidence page'
        : 'Loading application'

  if (isEvidenceRoute && publicStudioEnabled(pathname, search)) {
    return <EvidenceLoadingState />
  }

  if (publicStudioEnabled(pathname, search) && (isPublicRoute || isEmbedRoute || pathname === '/')) {
    return (
        <main className="studio-surface studio-surface-status" data-studio-theme={resolveStudioPaletteSync()}>
        <section className="studio-status-card" role="status" aria-live="polite">
          <BrandLockup variant="stacked" />
          <p className="studio-surface-eyebrow">{surfaceLabel}</p>
          <strong>{heading}</strong>
          <p className="muted">Preparing the client bundle and runtime state.</p>
          <div className="studio-status-pulse" aria-hidden="true"><span /></div>
        </section>
      </main>
    )
  }

  return (
    <main className="login-screen">
      <section className="login-card">
        <div className="login-branding">
          <BrandLockup variant="stacked" />
          <p className="brand-tagline">Evidence, located.</p>
        </div>
        <BrandMotion name="assemble" size={112} className="login-loading-mark brand-motion-glow" />
        <p className="kicker">{surfaceLabel}</p>
        <h1>{heading}</h1>
        <p className="muted">Preparing the client bundle and runtime state.</p>
      </section>
    </main>
  )
}

function boundaryConfigForPath(pathname) {
  if (pathname === '/public' || pathname.startsWith('/public/')) {
    return {
      viewName: 'Public browse',
      kicker: 'Loci Public',
      title: 'Something went wrong loading the public browse view.',
      message: 'The published-object browser could not finish rendering. Reload the page or return to the public entry surface.',
      backHref: '/public',
    }
  }

  if (pathname.startsWith('/embed/object/')) {
    return {
      viewName: 'Embed viewer',
      kicker: 'Loci Embed',
      title: 'Something went wrong loading this embed.',
      message: 'The embedded 3D viewer hit a client-side error. Reload the page or return to the public browse surface.',
      backHref: '/public',
    }
  }

  if (pathname.startsWith('/evidence/objects/')) {
    return {
      viewName: 'Evidence page',
      kicker: 'Loci Evidence',
      title: 'Something went wrong loading this evidence page.',
      message: 'The evidence view hit a client-side error. Reload the page or return to the public browse surface.',
      backHref: '/public',
    }
  }

  return {
    viewName: 'Console',
    kicker: 'Loci Console',
    title: 'Something went wrong loading the application.',
    message: 'Reload the page to try the console again.',
    backHref: '/',
  }
}

function rootInstanceKeyForLocation(pathname, search) {
  if (pathname.startsWith('/evidence/objects/')) {
    return pathname
  }

  if (pathname === '/public' || pathname === '/public/') {
    const params = new URLSearchParams(search || '')
    params.delete('object_page')
    return `${pathname}?${params.toString()}`
  }

  return `${pathname}${search || ''}`
}

function RootRouterApp() {
  const [locationState, setLocationState] = useState(() => readWindowLocation())

  useEffect(() => subscribeToLocationChanges(() => setLocationState(readWindowLocation())), [])

  const { pathname, search } = locationState
  useLayoutEffect(() => {
    if (typeof document === 'undefined') return
    if (publicStudioEnabled(pathname, search)) {
      document.documentElement.dataset.studioBootstrap = resolveStudioPaletteSync()
    } else {
      delete document.documentElement.dataset.studioBootstrap
    }
  }, [pathname, search])
  const ActiveRootApp = rootAppForPath(pathname, search)
  const boundaryConfig = boundaryConfigForPath(pathname)
  const rootInstanceKey = rootInstanceKeyForLocation(pathname, search)

  return (
    <Suspense fallback={<RootLoadingFallback pathname={pathname} search={search} />}>
      <ViewErrorBoundary key={rootInstanceKey} {...boundaryConfig}>
        <ActiveRootApp key={rootInstanceKey} />
      </ViewErrorBoundary>
    </Suspense>
  )
}

// Anchor the initialization so Rollup keeps it in the public multi-entry bundle.
globalThis.__loci_main_monitoring__ = initializeMonitoring()

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RootErrorBoundary>
      <RootRouterApp />
    </RootErrorBoundary>
  </React.StrictMode>
)
