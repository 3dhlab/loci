/* Studio P4.7 — shared Studio visual scope for the public surfaces (public
 * browse, landing decision surface, embed stage, error/empty/loading).
 * The approved Studio UI is the default public experience after production
 * launch; `?studio=0` or `?legacy=1` remains a visual rollback escape hatch.
 *
 * Design intent (see docs/studio-interface-redesign-canonical-2026-06-25.md):
 * the object-page Studio (StudioShell) is the reference. This module deliberately
 * does NOT import StudioShell so that object-page Studio (commit 90ad533) stays
 * untouched; it re-implements only the small chrome/theme pieces the other
 * surfaces need. The heavy visual work is CSS scoped under `.studio-surface`.
 *
 * Security note: nothing here adds 3D/wasm/blob or inline scripts — it is pure
 * React + CSS, so it is safe under every current per-route CSP, including the
 * tight `/` landing CSP.
 */
import { useCallback, useEffect, useState } from 'react'

import BrandLockup from '../components/BrandLockup'
import { PUBLIC_BROWSE_PATH, navigateToPublicBrowse } from '../lib/navigation'
import { applyDocumentThemeChrome } from '../lib/documentTheme'

const THEME_STORAGE_KEY = 'loci.studio.theme'
const THEME_MODES = ['system', 'light', 'dark']

function readSearchParams(search) {
  const raw = typeof search === 'string'
    ? search
    : (typeof window !== 'undefined' ? window.location.search : '')
  try {
    return new URLSearchParams(raw || '')
  } catch {
    return new URLSearchParams()
  }
}

// Public Studio is now default-on. Explicit opt-out params keep an immediate
// rollback path for production verification without changing deploy artifacts.
export function isStudioSurfaceEnabled(search, { defaultEnabled = false } = {}) {
  const params = readSearchParams(search)
  const studio = (params.get('studio') || '').trim().toLowerCase()
  const legacy = (params.get('legacy') || '').trim().toLowerCase()

  if (['0', 'false', 'off', 'no'].includes(studio) || ['1', 'true', 'on', 'yes'].includes(legacy)) {
    return false
  }

  if (['1', 'true', 'on', 'yes'].includes(studio)) {
    return true
  }

  return Boolean(defaultEnabled)
}

function readStoredMode() {
  if (typeof window === 'undefined') {
    return 'system'
  }
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return THEME_MODES.includes(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

function systemPrefersDark() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

// `light` → Cobalt, `dark` → Darkroom, `system` → follows the OS preference.
function resolvePalette(mode, prefersDark) {
  if (mode === 'light') {
    return 'cobalt'
  }
  if (mode === 'dark') {
    return 'darkroom'
  }
  return prefersDark ? 'darkroom' : 'cobalt'
}

// Synchronous, one-shot palette read for surfaces that need the Studio palette
// outside a React render subscription (e.g. the evidence not-found branch, which
// renders before the component's normal hooks). No listener; resolves from the
// same stored mode + OS preference the hook uses.
export function resolveStudioPaletteSync() {
  return resolvePalette(readStoredMode(), systemPrefersDark())
}

// Shares the object page's persistence key so the theme choice carries across
// every Studio surface. System preference remains the internal default.
export function useStudioSurfaceTheme() {
  const [mode, setMode] = useState(readStoredMode)
  const [prefersDark, setPrefersDark] = useState(systemPrefersDark)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined
    }
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = (event) => setPrefersDark(event.matches)
    setPrefersDark(mediaQuery.matches)
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    }
    mediaQuery.addListener(handleChange)
    return () => mediaQuery.removeListener(handleChange)
  }, [])

  const selectMode = useCallback((nextMode) => {
    if (!THEME_MODES.includes(nextMode)) {
      return
    }
    setMode(nextMode)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextMode)
    } catch {
      // Best-effort; the in-memory mode still applies.
    }
  }, [])

  const palette = resolvePalette(mode, prefersDark)

  // Keep mobile browser chrome + safe-area fill in sync with the Studio palette
  // on the public surfaces (P4.8-C #4).
  useEffect(() => {
    if (typeof document !== 'undefined' && isStudioSurfaceEnabled(undefined, { defaultEnabled: true })) {
      document.documentElement.dataset.studioBootstrap = palette
    }
    applyDocumentThemeChrome(palette)
  }, [palette])

  const toggle = useCallback(() => {
    selectMode(palette === 'darkroom' ? 'light' : 'dark')
  }, [palette, selectMode])

  return { mode, palette, selectMode, toggle }
}

// Minimal light/dark icon toggle — identical language to the object page.
export function StudioThemeToggle({ palette, onToggle }) {
  const isDark = palette === 'darkroom'
  return (
    <button
      type="button"
      className="studio-theme-toggle"
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-pressed={isDark}
      onClick={onToggle}
    >
      {isDark ? (
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="4.2" fill="currentColor" />
          <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <line x1="12" y1="2.5" x2="12" y2="5" />
            <line x1="12" y1="19" x2="12" y2="21.5" />
            <line x1="2.5" y1="12" x2="5" y2="12" />
            <line x1="19" y1="12" x2="21.5" y2="12" />
            <line x1="5.2" y1="5.2" x2="6.9" y2="6.9" />
            <line x1="17.1" y1="17.1" x2="18.8" y2="18.8" />
            <line x1="5.2" y1="18.8" x2="6.9" y2="17.1" />
            <line x1="17.1" y1="6.9" x2="18.8" y2="5.2" />
          </g>
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.8 6.8 0 0 0 9.8 9.8z" fill="currentColor" />
        </svg>
      )}
    </button>
  )
}

// Shared Studio chrome for the non-object surfaces: wordmark → home + theme
// toggle. `brandHome` links the wordmark to the public browse home (fixes the
// inventory's brand-home gap); `onReturnHome` overrides the default navigation.
export function StudioSurfaceChrome({
  palette,
  onToggle,
  eyebrow = '',
  title = '',
  brandHome = true,
  onReturnHome,
  extra = null,
}) {
  const goHome = (event) => {
    if (!brandHome) {
      return
    }
    event.preventDefault()
    if (typeof onReturnHome === 'function') {
      onReturnHome()
      return
    }
    navigateToPublicBrowse()
  }

  return (
    <header className="studio-surface-chrome">
      <div className="studio-surface-chrome-row">
        {brandHome ? (
          <a
            className="studio-surface-brand"
            href={PUBLIC_BROWSE_PATH}
            aria-label="Go to the public homepage"
            onClick={goHome}
          >
            <BrandLockup variant="horizontal" />
          </a>
        ) : (
          <span className="studio-surface-brand" aria-hidden="true">
            <BrandLockup variant="horizontal" />
          </span>
        )}
        {title ? (
          <div className="studio-surface-chrome-title">
            {eyebrow ? <span className="studio-surface-eyebrow">{eyebrow}</span> : null}
            <strong>{title}</strong>
          </div>
        ) : null}
        <div className="studio-surface-chrome-controls">
          {extra}
          <a
            className="studio-return-home studio-surface-return-home"
            href={PUBLIC_BROWSE_PATH}
            onClick={(event) => {
              event.preventDefault()
              if (typeof onReturnHome === 'function') {
                onReturnHome()
                return
              }
              navigateToPublicBrowse()
            }}
          >
            Return to homepage
          </a>
          <StudioThemeToggle palette={palette} onToggle={onToggle} />
        </div>
      </div>
    </header>
  )
}
