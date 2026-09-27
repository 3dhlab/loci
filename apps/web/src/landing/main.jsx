import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Shared base tokens + typography + utility classes from the canonical stylesheet.
// The landing reuses every token defined here; introduces no new tokens (per Slice A
// of public-landing-session-2026-04-23.md and Type & spacing spec in
// design/landing-page-mockup-2026-04-23/README.md).
import '../styles.css'

// Landing-specific layout, hero stagger keyframes, converge one-shot keyframes,
// and prefers-reduced-motion fallback.
import './styles.css'

import LandingApp from './LandingApp.jsx'
import { initializeMonitoring } from '../lib/monitoring'

// Anchor the call to a globalThis property so Rollup keeps it.
// Without the anchor, Vite/Rollup tree-shook the entire monitoring
// import + call from landing-*.js (Lane A Task 3 diagnosis 2026-04-28),
// because static analysis determined initializeMonitoring()'s only
// observable effects (module-level state mutation, void-wrapped async
// init) were not reachable from outside the module.
globalThis.__loci_landing_monitoring__ = initializeMonitoring()

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('Landing root element #root not found')
}

createRoot(rootElement).render(
  <StrictMode>
    <LandingApp />
  </StrictMode>,
)
