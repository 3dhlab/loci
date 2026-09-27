/* Studio P4.7.1 — flag-gated public landing preview.
 *
 * Founder review: the live editorial landing (LandingApp) is the desired layout.
 * This preview reuses that hero composition — centered converge mark, tagline,
 * headline, framing paragraph, "Browse the collection" CTA, search, stats, and
 * the notify form — and applies the Studio Cobalt/Darkroom palette plus a minimal
 * light/dark icon toggle. Shown ONLY at `/?studio=1`; with the flag off, `/`
 * renders the operator console exactly as today.
 *
 * CSP-safe for the tight `/` policy: pure React + CSS, no 3D/wasm/blob, no inline
 * scripts. It does NOT relax or require any CSP change. Landing styling lives in
 * apps/web/src/styles.css scoped under `.studio-landing` (landing/styles.css is a
 * separate entry bundle and is intentionally not imported here).
 */
import { useEffect } from 'react'

import PublicStatsPanel from '../components/PublicStatsPanel'
import ConvergeMark from '../landing/components/ConvergeMark.jsx'
import NotifyCapture from '../landing/components/NotifyCapture.jsx'
import SearchInput from '../landing/components/SearchInput.jsx'
import { PUBLIC_BROWSE_PATH, navigateToPublicBrowse } from '../lib/navigation'
import { setPageMeta } from '../lib/seo'
import { StudioThemeToggle, useStudioSurfaceTheme } from './studioSurface'

// Headline + framing copy are carried verbatim from the live landing (LandingApp)
// so the preview reads identically; only the palette changes.
const HEADLINE = 'Interpretation fixed to where it happened, retrievable by place and by meaning.'

const FRAMING =
  'Loci anchors every interpretive moment to a recording, a transcript line, and a location on the object it refers to. Browse the collection to open evidence pages with synchronized video, transcript, and 3D context — or search for a word, phrase, or recorded moment and move directly into the matching page.'

export default function StudioLandingPreview({ clientNavigation = true }) {
  const { palette, toggle } = useStudioSurfaceTheme()

  useEffect(() => {
    setPageMeta({
      title: 'Evidence, located.',
      description: 'Evidence, located. Explore published objects with synchronized 3D, video, and transcript context.',
      url: typeof window !== 'undefined' ? window.location.href : '',
      updateDocumentTitle: true,
    })
  }, [])

  const openBrowse = (event) => {
    if (!clientNavigation) {
      return
    }

    if (event) {
      event.preventDefault()
    }
    navigateToPublicBrowse()
  }

  return (
    <main className="studio-surface studio-landing" data-studio-theme={palette}>
      {/* Minimal top bar: brand-home wordmark left, light/dark toggle right. */}
      <div className="studio-landing-topbar">
        <a
          className="studio-landing-brand"
          href={PUBLIC_BROWSE_PATH}
          aria-label="Go to the public collection"
          onClick={openBrowse}
        >
          <ConvergeMark animated={false} size={26} ariaLabel="Loci" />
          <span className="studio-landing-brand-word">Loci</span>
        </a>
        <StudioThemeToggle palette={palette} onToggle={toggle} />
      </div>

      <div className="studio-landing-hero-wrap">
        <section className="studio-landing-hero" aria-label="Loci editorial intro">
          <div className="studio-landing-mark">
            <ConvergeMark animated size={76} ariaLabel="Loci converge — animated mark" />
          </div>

          <p className="studio-landing-tagline">Evidence, located.</p>

          <h1 className="studio-landing-headline">{HEADLINE}</h1>

          <p className="studio-landing-framing">{FRAMING}</p>

          <div className="studio-landing-actions">
            <a className="studio-landing-cta" href={PUBLIC_BROWSE_PATH} onClick={openBrowse}>
              Browse the collection
            </a>
            <SearchInput />
          </div>

          <div className="studio-landing-stats">
            <PublicStatsPanel />
          </div>

          <div className="studio-landing-notify">
            <NotifyCapture />
          </div>
        </section>
      </div>
    </main>
  )
}
