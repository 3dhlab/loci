/* Sync mobile browser chrome to the active Studio palette (P4.8-C #4).
 *
 * Drives three things from the current Studio palette so iOS/Android chrome and
 * safe areas follow light/dark instead of staying light:
 *   - <meta name="theme-color">  -> Safari/Chrome status-bar + address-bar tint
 *   - document.documentElement.style.colorScheme -> form controls / scrollbars
 *   - html + body background     -> the fill iOS paints in the notch / home-
 *                                   indicator safe-area insets and overscroll
 *
 * Values come from a FIXED palette map keyed by the Studio palette enum
 * ('cobalt' | 'darkroom') — never from user input — so there is no DOM/CSS
 * injection surface. Best-effort and SSR-safe; failures never break rendering.
 */
const PALETTE_CHROME = {
  cobalt: { themeColor: '#eef2f8', colorScheme: 'light', background: '#eef2f8' },
  'muted-light': { themeColor: '#f2f3f1', colorScheme: 'light', background: '#f2f3f1' },
  darkroom: { themeColor: '#0f1620', colorScheme: 'dark', background: '#0c121b' },
}

function ensureThemeColorMeta() {
  // The HTML ships media-scoped theme-color metas for correct first paint before
  // JS runs. Once we take over, a matching media meta would win over a plain one
  // (spec picks the first meta whose media matches), so drop the media variants
  // and own a single authoritative meta reflecting the in-app palette.
  document.querySelectorAll('meta[name="theme-color"][media]').forEach((node) => node.remove())
  let meta = document.querySelector('meta[name="theme-color"]:not([media])')
  if (!meta) {
    meta = document.createElement('meta')
    meta.setAttribute('name', 'theme-color')
    document.head.appendChild(meta)
  }
  return meta
}

export function applyDocumentThemeChrome(palette) {
  if (typeof document === 'undefined') {
    return
  }
  const chrome = PALETTE_CHROME[palette] || PALETTE_CHROME.cobalt
  try {
    ensureThemeColorMeta().setAttribute('content', chrome.themeColor)
    const root = document.documentElement
    root.style.colorScheme = chrome.colorScheme
    root.style.backgroundColor = chrome.background
    if (document.body) {
      document.body.style.backgroundColor = chrome.background
    }
  } catch {
    // Chrome sync is cosmetic; never let it throw into render.
  }
}
