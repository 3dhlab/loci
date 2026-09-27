/*
 * PartnerChrome
 *
 * Top-of-page partner co-signature per BRAND.md §10:
 *   [Customer Wordmark] | [Loci mini-mark] Loci   |   [Sign in]
 *
 * The customer leads at their natural register; Loci is the co-signed
 * instrument signature. Loci mini-mark + label is always smaller than
 * the customer wordmark.
 *
 * Below 380px viewport the "Loci" word-label collapses to icon-only
 * (handled by CSS in landing/styles.css). Sign-in stays untouched
 * per founder decision #11 / brief §5.6.
 */
import ConvergeMark from './ConvergeMark.jsx'
import SignInAffordance from './SignInAffordance.jsx'

export default function PartnerChrome({ customerName = '3DZ' }) {
  return (
    <header className="partner-chrome" role="banner">
      <div className="partner-chrome__left">
        <span className="partner-chrome__customer" aria-label={`${customerName} — operating partner`}>
          {customerName}
        </span>
        <span className="partner-chrome__divider" aria-hidden="true" />
        <span className="partner-chrome__loci">
          <span className="partner-chrome__loci-mark">
            <ConvergeMark animated={false} size={22} ariaLabel="Loci" />
          </span>
          <span className="partner-chrome__loci-label">Loci</span>
        </span>
      </div>
      <SignInAffordance />
    </header>
  )
}
