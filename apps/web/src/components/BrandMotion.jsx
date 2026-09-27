export default function BrandMotion({ name = 'assemble', className = '', decorative = true, label = '', size = 64 }) {
  const source = `/branding/animations/${name}.svg`
  const classes = ['brand-motion', `brand-motion-${name}`, className].filter(Boolean).join(' ')

  return (
    <img
      className={classes}
      src={source}
      alt={decorative ? '' : (label || 'Loci motion')}
      aria-hidden={decorative || undefined}
      width={size}
      height={size}
      decoding="async"
      loading="eager"
    />
  )
}