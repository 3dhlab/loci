import React from 'react'
import { captureViewError } from '../lib/monitoring'

// Contains 3D viewer failures (e.g. "THREE.WebGLRenderer: Error creating WebGL
// context") inside the viewer pane so the rest of the page (video, transcript,
// citations, nav) stays usable. Falls back to a compact in-pane message with a
// recoverable retry instead of escalating to the global full-screen crash view.
export default class ModelViewerErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
    this.reset = this.reset.bind(this)
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Model viewer render error:', error)
    if (typeof this.props.onError === 'function') {
      this.props.onError(error)
    }
    try {
      captureViewError(error, {
        viewName: 'Model viewer',
        route: typeof window !== 'undefined' ? window.location.pathname : '',
        componentStack: info?.componentStack || '',
      })
    } catch (telemetryError) {
      // Telemetry must never rethrow and take down the boundary.
      console.error('Model viewer telemetry failed:', telemetryError)
    }
  }

  reset() {
    this.setState({ error: null })
    if (typeof this.props.onReset === 'function') {
      this.props.onReset()
    }
  }

  render() {
    if (!this.state.error) {
      return this.props.children
    }

    return (
      <div
        role="alert"
        style={{
          boxSizing: 'border-box',
          width: '100%',
          height: '100%',
          minHeight: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '0.75rem',
          padding: '1.5rem',
          textAlign: 'center',
          color: 'inherit',
        }}
      >
        <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 600 }}>3D viewer unavailable</h2>
        <p style={{ margin: 0, maxWidth: '32ch', opacity: 0.8 }}>
          LOCI could not load a compatible 3D model for this device or connection. Video, transcript, and citations remain available.
        </p>
        <button
          type="button"
          className="model-viewer-error-retry"
          onClick={this.reset}
          style={{
            marginTop: '0.25rem',
            padding: '0.45rem 1.1rem',
            borderRadius: '0.5rem',
            border: '1px solid currentColor',
            background: 'transparent',
            color: 'inherit',
            cursor: 'pointer',
            font: 'inherit',
          }}
        >
          Try again
        </button>
      </div>
    )
  }
}
