/*
 * Footer
 *
 * Minimal one-line attribution per founder decision #10:
 *   "Operated by 3DZ · Loci"
 *
 * Inter 12px, color --ink-soft. The 14px Loci mini-mark sits before
 * the "Loci" wordmark per BRAND.md §10 (signature pattern).
 *
 * No legal boilerplate, no sitemap, no social icons on the dev instance.
 * Per-customer overrides can append a privacy/terms link in subsequent
 * partner deploys without code changes (just compose this component
 * with extra children).
 */
import ConvergeMark from './ConvergeMark.jsx'

export default function Footer({ customerName = '3DZ' }) {
  return (
    <footer className="landing-footer" role="contentinfo">
      <span>Operated by {customerName} · </span>
      <span className="landing-footer__mark">
        <ConvergeMark animated={false} size={14} ariaLabel="" />
      </span>
      <span>Loci</span>
    </footer>
  )
}
