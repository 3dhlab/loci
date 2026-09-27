/*
 * SearchInput
 *
 * Wraps SearchAutosuggest for the landing surface.
 *
 * Lane B swap (2026-04-29):
 *   - Uses the live /api/v1/public/search/suggest resolver via the shared
 *     SearchAutosuggest component.
 *   - Keeps LANDING_SEARCH_POOL as graceful degradation when the request
 *     fails (network error or non-OK response).
 *   - No auth token required; the shared resolver falls back to the public
 *     endpoint when token is absent.
 *
 * onSubmit (Contract 3):
 *   - Empty value      → window.location.href = '/public'
 *   - Non-empty value  → window.location.href = '/public?q=<encoded>'
 */
import { useState } from 'react'

import SearchAutosuggest from '../../components/SearchAutosuggest.jsx'
import LANDING_SEARCH_POOL from '../staticSuggestions.js'

export default function SearchInput() {
  const [value, setValue] = useState('')

  const handleSubmit = (submitted) => {
    const trimmed = (submitted ?? value ?? '').trim()
    if (trimmed) {
      window.location.href = `/public?q=${encodeURIComponent(trimmed)}`
    } else {
      window.location.href = '/public'
    }
  }

  return (
    <div className="landing-search">
      <form
        className="landing-search__form"
        role="search"
        onSubmit={(e) => {
          e.preventDefault()
          handleSubmit()
        }}
        noValidate
      >
        <div className="landing-search__field">
          <SearchAutosuggest
            value={value}
            onChange={setValue}
            onSubmit={handleSubmit}
            placeholder="Search published transcript moments"
            staticPool={LANDING_SEARCH_POOL}
            limit={7}
          />
        </div>
        <button type="submit" className="landing-search__submit" aria-label="Search the collection">
          Search
        </button>
      </form>
    </div>
  )
}
