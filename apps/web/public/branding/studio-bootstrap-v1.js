/* Parser-blocking, CSP-safe first-paint palette for public Studio routes. */
(() => {
  const path = window.location.pathname || '/'
  const publicRoute = path === '/' || path === '/public' || path.startsWith('/public/') ||
    path.startsWith('/embed/object/') || path.startsWith('/evidence/objects/')
  if (!publicRoute) return

  const params = new URLSearchParams(window.location.search || '')
  const studio = (params.get('studio') || '').trim().toLowerCase()
  const legacy = (params.get('legacy') || '').trim().toLowerCase()
  if (['0', 'false', 'off', 'no'].includes(studio) || ['1', 'true', 'on', 'yes'].includes(legacy)) return

  let mode = 'system'
  try {
    const saved = window.localStorage.getItem('loci.studio.theme')
    if (['system', 'light', 'dark'].includes(saved)) mode = saved
  } catch { /* Private browsing and storage restrictions use system mode. */ }
  const dark = mode === 'dark' || (mode === 'system' &&
    typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  const theme = dark ? 'darkroom' : 'cobalt'
  document.documentElement.dataset.studioBootstrap = theme
  const themeColors = document.querySelectorAll('meta[name="theme-color"]')
  themeColors.forEach((meta) => { meta.media = ''; meta.content = dark ? '#0f1620' : '#eef2f8' })
})()
