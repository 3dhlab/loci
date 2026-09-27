/*
 * PublicStatsPanel
 *
 * Shared component used by the editorial landing page and /public — both
 * surfaces source their visible "open now" count from the same component
 * hitting the same endpoint, fulfilling Contract 2 of
 * public-landing-session-2026-04-23.md.
 *
 * Renders the open-now count using the sentence framing locked by founder
 * decision #15:
 *   "1 object currently open for evidence review"
 *
 * Endpoint: GET /api/v1/public/stats (no auth, 60s server-side cache).
 * Helper:   fetchPublicStats() in apps/web/src/lib/api.js.
 *
 * If the endpoint is unreachable, the component renders a neutral
 * unavailable state so public outages stay visible during runtime QA.
 */
import { useEffect, useState } from 'react'

import { fetchPublicStats } from '../lib/api'

export default function PublicStatsPanel() {
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let aborted = false

    fetchPublicStats()
      .then((data) => {
        if (aborted) return
        setStats(data)
      })
      .catch((err) => {
        if (aborted) return
        setError({ message: err?.message || 'stats unavailable', status: err?.status ?? null })
      })

    return () => {
      aborted = true
    }
  }, [])

  if (error && !stats) {
    // This panel only reports the "open now" count and is decoupled from the
    // object cards, so a fetch failure must never read as a collection outage.
    // Transient statuses (429/503) and network errors (no status) render as a
    // quiet "updating" line; anything else stays subdued and neutral.
    const transient = error.status === 429 || error.status === 503 || !error.status
    const message = transient
      ? 'Live collection count is updating…'
      : 'Live collection count unavailable'
    return (
      <div className="public-stats" data-state="unavailable">
        <p className="public-stats__loading">{message}</p>
      </div>
    )
  }

  if (!stats) {
    return (
      <div className="public-stats" data-state="loading">
        <p className="public-stats__loading">Loading collection stats…</p>
      </div>
    )
  }

  const openNow = stats.open_now_count ?? 0
  const objectWord = openNow === 1 ? 'object' : 'objects'

  return (
    <div className="public-stats" data-state="ready">
      <p className="public-stats__sentence">
        {openNow} {objectWord} currently open for evidence review
      </p>
    </div>
  )
}
