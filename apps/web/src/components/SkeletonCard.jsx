import BrandLockup from './BrandLockup'

export default function SkeletonCard({
  kicker = '',
  title,
  message,
  headingAs = 'h2',
  showBranding = false,
  showActionPlaceholder = false,
  className = '',
  blocks = ['stage', 'detail', 'detail']
}) {
  const HeadingTag = headingAs

  return (
    <section className={`skeleton-card ${showBranding ? 'with-branding' : ''} ${className}`.trim()} role="status" aria-live="polite">
      {showBranding ? (
        <div className="login-branding">
          <BrandLockup variant="stacked" />
          <p className="brand-tagline">Evidence, located.</p>
        </div>
      ) : null}

      <div className="skeleton-card-copy">
        {kicker ? <p className="kicker">{kicker}</p> : null}
        <HeadingTag>{title}</HeadingTag>
        <p className="muted">{message}</p>
      </div>

      <div className="skeleton-card-grid" aria-hidden="true">
        {blocks.map((block, index) => (
          <span key={`${block}:${index}`} className={`skeleton-card-block ${block}`.trim()} />
        ))}
      </div>

      {showActionPlaceholder ? (
        <div className="public-library-card-actions" aria-hidden="true">
          <button type="button" className="search-result-button" disabled aria-hidden="true" tabIndex={-1}>
            Open evidence
          </button>
        </div>
      ) : null}
    </section>
  )
}