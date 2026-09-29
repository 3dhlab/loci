import BrandMotion from '../components/BrandMotion'
import { resolveStudioPaletteSync } from '../public/studioSurface'

export const EVIDENCE_MAIN_CONTENT_ID = 'evidence-main-content'

function focusEvidenceMainContent() {
  if (typeof document === 'undefined') return
  const target = document.getElementById(EVIDENCE_MAIN_CONTENT_ID)
  if (!(target instanceof HTMLElement)) return
  window.requestAnimationFrame(() => target.focus())
}

export default function EvidenceLoadingState() {
  return (
    <>
      <a className="skip-link" href={`#${EVIDENCE_MAIN_CONTENT_ID}`} onClick={focusEvidenceMainContent}>
        Skip to content
      </a>
      <main
        id={EVIDENCE_MAIN_CONTENT_ID}
        tabIndex={-1}
        className="studio-surface studio-surface-status evidence-loading-screen skip-link-target"
        data-studio-theme={resolveStudioPaletteSync()}
      >
        <section className="evidence-loading-indicator" role="status" aria-live="polite" aria-atomic="true">
          <BrandMotion name="pivot" size={72} className="brand-motion-glow" />
          <span>Loading…</span>
        </section>
      </main>
    </>
  )
}
