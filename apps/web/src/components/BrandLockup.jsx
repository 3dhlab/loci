export default function BrandLockup({ variant = 'horizontal', className = '', decorative = false }) {
  const source = variant === 'stacked'
    ? '/branding/lockup-stacked.svg'
    : '/branding/lockup-horizontal.svg'
  const classes = ['brand-lockup', `brand-lockup-${variant}`, className].filter(Boolean).join(' ')

  return (
    <img
      className={classes}
      src={source}
      alt={decorative ? '' : 'Loci'}
      aria-hidden={decorative || undefined}
      decoding="async"
    />
  )
}