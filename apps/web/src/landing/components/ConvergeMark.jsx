/*
 * ConvergeMark
 *
 * Inline-SVG port of branding/animations/converge.svg with two operating modes:
 *   - animated={true} (default) → one-shot landing animation per Contract 1.
 *     Children get .lc-z, .lc-x, .lc-y, .lc-locus classes; the keyframes
 *     declared in landing/styles.css drive a 1500ms swing-in that holds
 *     the formed state on completion (no loop). prefers-reduced-motion
 *     disables animation and forces formed state.
 *
 *   - animated={false} → static formed mark for partner chrome + footer.
 *
 * Geometry, colors, and stroke widths port verbatim from the canonical SVG.
 * Axis colors (--axis-z navy, --axis-x rust, --axis-y olive) appear ONLY
 * inside this SVG, never in chrome.
 */
export default function ConvergeMark({ animated = true, size = 76, ariaLabel = 'Loci converge mark' }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 64 64"
      width={size}
      height={size}
      role="img"
      aria-label={ariaLabel}
      focusable="false"
    >
      <g strokeLinecap="round">
        <g className={animated ? 'lc-z' : undefined}>
          <line x1="32" y1="37" x2="32" y2="17" stroke="#2F4A6D" strokeWidth="4" />
          <circle cx="32" cy="17" r="4.5" fill="#2F4A6D" />
        </g>
        <g className={animated ? 'lc-x' : undefined}>
          <line x1="32" y1="37" x2="49.32" y2="47" stroke="#B84A2B" strokeWidth="4" />
          <circle cx="49.32" cy="47" r="4.5" fill="#B84A2B" />
        </g>
        <g className={animated ? 'lc-y' : undefined}>
          <line x1="32" y1="37" x2="14.68" y2="47" stroke="#5A7A3D" strokeWidth="4" />
          <circle cx="14.68" cy="47" r="4.5" fill="#5A7A3D" />
        </g>
      </g>
      <circle className={animated ? 'lc-locus' : undefined} cx="32" cy="37" r="4.5" fill="#ca5f22" />
    </svg>
  )
}
