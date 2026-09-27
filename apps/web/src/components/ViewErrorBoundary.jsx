import React from 'react'
import BrandLockup from './BrandLockup'
import { captureViewError } from '../lib/monitoring'
import { isStudioSurfaceEnabled, resolveStudioPaletteSync } from '../public/studioSurface'

export default class ViewErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, errorInfo) {
    console.error(`${this.props.viewName || 'View'} render error:`, error)
    captureViewError(error, {
      viewName: this.props.viewName || 'View',
      route: typeof window !== 'undefined' ? window.location.pathname : '',
      componentStack: errorInfo?.componentStack || '',
    })
  }

  render() {
    if (!this.state.error) {
      return this.props.children
    }

    const backHref = this.props.backHref || '/public'
    // Public routes use the approved Studio crash palette by default; explicit
    // `?studio=0` / `?legacy=1` keeps the legacy fallback available.
    const pathname = typeof window !== 'undefined' ? window.location.pathname : ''
    const isPublicRoute = (
      pathname === '/' ||
      pathname === '/public' ||
      pathname.startsWith('/public/') ||
      pathname.startsWith('/embed/object/') ||
      pathname.startsWith('/evidence/objects/')
    )
    const studioEnabled = isStudioSurfaceEnabled(undefined, { defaultEnabled: isPublicRoute })
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
          <p className="kicker">{this.props.kicker || 'Loci Public'}</p>
          <h1>{this.props.title || 'Something went wrong loading this page.'}</h1>
          <p className="muted">{this.props.message || 'Reload the page or return to the public browse surface.'}</p>
          <div style={{ display: 'flex', gap: '0.75rem', marginTop: '1rem', alignItems: 'center', justifyContent: 'center', flexWrap: 'wrap' }}>
            <button type="button" className="ghost" onClick={() => window.location.reload()}>Try again</button>
            <a href={backHref}>Go back to browse</a>
          </div>
          {isPublicRoute ? (
            <p className="muted" role="status">Technical details stay private and are recorded through the configured error monitor.</p>
          ) : (
            <details className="result-box" style={{ marginTop: '1rem', textAlign: 'left' }}>
              <summary>Error details</summary>
              <pre style={{ whiteSpace: 'pre-wrap' }}>{String(this.state.error.message || this.state.error)}</pre>
            </details>
          )}
        </section>
      </main>
    )
  }
}
