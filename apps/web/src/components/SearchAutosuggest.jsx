import { useEffect, useId, useMemo, useRef, useState } from 'react'

import { fetchSearchSuggestions } from '../lib/api'

const DEBOUNCE_MS = 150
const MIN_PREFIX_LENGTH = 2
const GHOST_SCORE_THRESHOLD = 0.22
const MOBILE_MAX_WIDTH_PX = 600

export default function SearchAutosuggest({
  value,
  onChange,
  onSubmit,
  projectId,
  token,
  placeholder,
  disabled = false,
  limit = 7,
  inputRef: externalInputRef = null,
  inputProps = {},
  staticPool = null,
  onClear,
  clearLabel = 'Clear search',
}) {
  const [suggestions, setSuggestions] = useState([])
  const [open, setOpen] = useState(false)
  const [highlightIndex, setHighlightIndex] = useState(-1)
  const [isComposing, setIsComposing] = useState(false)
  const [ghostDismissed, setGhostDismissed] = useState(false)
  const [isMobile, setIsMobile] = useState(false)
  const abortRef = useRef(null)
  const debounceTimerRef = useRef(null)
  const focusedRef = useRef(false)
  const inputRef = useRef(null)
  const listboxId = useId()
  const { type: inputType = 'text', ...resolvedInputProps } = inputProps

  const trimmedQuery = (value || '').trim()
  const hasStaticFallback = Array.isArray(staticPool) && staticPool.length > 0
  const hasClearAction = Boolean(value) && (typeof onClear === 'function' || typeof onChange === 'function')

  function resolveStaticSuggestions(query) {
    const q = String(query || '').toLowerCase()
    return staticPool
      .map((phrase) => {
        const normalizedPhrase = String(phrase).toLowerCase()
        let score = 0
        if (normalizedPhrase.startsWith(q)) score = 1.0
        else if (normalizedPhrase.includes(q)) score = 0.7
        return { phrase_text: phrase, score, source: 'static' }
      })
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, limit)
  }

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const mql = window.matchMedia(`(max-width: ${MOBILE_MAX_WIDTH_PX}px)`)
    const apply = () => setIsMobile(mql.matches)
    apply()
    mql.addEventListener('change', apply)
    return () => mql.removeEventListener('change', apply)
  }, [])

  useEffect(() => {
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current)
      debounceTimerRef.current = null
    }
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }

    if (trimmedQuery.length < MIN_PREFIX_LENGTH) {
      setSuggestions([])
      setHighlightIndex(-1)
      return () => {}
    }

    const controller = new AbortController()
    abortRef.current = controller

    debounceTimerRef.current = setTimeout(async () => {
      try {
        const response = await fetchSearchSuggestions(token, {
          query: trimmedQuery,
          projectId: projectId || undefined,
          limit,
          signal: controller.signal,
        })
        if (controller.signal.aborted) return
        const items = Array.isArray(response?.results) ? response.results : []
        setSuggestions(items)
        setHighlightIndex(items.length > 0 ? 0 : -1)
      } catch (err) {
        if (err?.name === 'AbortError') return
        if (hasStaticFallback) {
          const fallbackItems = resolveStaticSuggestions(trimmedQuery)
          setSuggestions(fallbackItems)
          setHighlightIndex(fallbackItems.length > 0 ? 0 : -1)
          return
        }
        setSuggestions([])
        setHighlightIndex(-1)
      }
    }, DEBOUNCE_MS)

    return () => {
      clearTimeout(debounceTimerRef.current)
      controller.abort()
    }
  }, [trimmedQuery, projectId, token, limit, hasStaticFallback, staticPool])

  useEffect(() => () => {
    clearTimeout(debounceTimerRef.current)
    abortRef.current?.abort()
  }, [])

  const shouldShowDropdown = useMemo(() => {
    return open && suggestions.length > 0 && trimmedQuery.length >= MIN_PREFIX_LENGTH
  }, [open, suggestions, trimmedQuery])

  const ghostSuggestion = useMemo(() => {
    if (!value || isComposing || isMobile || ghostDismissed) return null
    if (trimmedQuery.length < MIN_PREFIX_LENGTH) return null
    const top = suggestions[0]
    if (!top) return null
    if (typeof top.score === 'number' && top.score < GHOST_SCORE_THRESHOLD) return null
    const topLower = (top.phrase_text || '').toLowerCase()
    const valueLower = value.toLowerCase()
    if (!topLower.startsWith(valueLower)) return null
    if (topLower === valueLower) return null
    return {
      phrase_text: top.phrase_text,
      completion: top.phrase_text.slice(value.length),
    }
  }, [value, suggestions, isComposing, isMobile, ghostDismissed, trimmedQuery])

  function commitSuggestion(phrase) {
    if (!phrase) return
    onChange?.(phrase)
    setSuggestions([])
    setHighlightIndex(-1)
    setOpen(false)
    setGhostDismissed(false)
    onSubmit?.(phrase)
  }

  function acceptGhost() {
    if (!ghostSuggestion) return false
    const full = ghostSuggestion.phrase_text
    onChange?.(full)
    setGhostDismissed(false)
    setOpen(false)
    setTimeout(() => inputRef.current?.focus(), 0)
    return true
  }

  function handleKeyDown(event) {
    if (event.key === 'ArrowDown') {
      if (suggestions.length === 0) return
      event.preventDefault()
      setOpen(true)
      setHighlightIndex((prev) => (prev + 1) % suggestions.length)
      return
    }
    if (event.key === 'ArrowUp') {
      if (suggestions.length === 0) return
      event.preventDefault()
      setOpen(true)
      setHighlightIndex((prev) => (prev <= 0 ? suggestions.length - 1 : prev - 1))
      return
    }
    if (event.key === 'Enter') {
      if (shouldShowDropdown && highlightIndex >= 0 && highlightIndex < suggestions.length) {
        event.preventDefault()
        commitSuggestion(suggestions[highlightIndex].phrase_text)
      }
      return
    }
    if (event.key === 'Escape') {
      if (shouldShowDropdown || ghostSuggestion) {
        event.preventDefault()
        setOpen(false)
        setGhostDismissed(true)
      }
      return
    }
    if (event.key === 'Tab') {
      if (ghostSuggestion) {
        event.preventDefault()
        acceptGhost()
        return
      }
      setOpen(false)
      return
    }
    if (event.key === 'ArrowRight') {
      if (!ghostSuggestion) return
      const input = inputRef.current
      if (!input) return
      const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length
      if (atEnd) {
        event.preventDefault()
        acceptGhost()
      }
      return
    }
    if (event.key === 'Backspace') {
      setGhostDismissed(true)
    }
  }

  function handleBlur() {
    focusedRef.current = false
    // Clear the ghost typeahead when focus leaves the field; a fresh keystroke
    // re-enables it (handleInputChange resets ghostDismissed).
    setGhostDismissed(true)
    setTimeout(() => {
      if (!focusedRef.current) {
        setOpen(false)
      }
    }, 120)
  }

  function handleFocus() {
    focusedRef.current = true
    if (suggestions.length > 0) setOpen(true)
  }

  function handleItemMouseDown(event, item) {
    event.preventDefault()
    focusedRef.current = true
    commitSuggestion(item.phrase_text)
  }

  function handleInputChange(event) {
    onChange?.(event.target.value)
    setOpen(true)
    setGhostDismissed(false)
  }

  function handleClear(event) {
    event?.preventDefault()
    event?.stopPropagation()
    focusedRef.current = true
    // Always empty the controlled input value via onChange('') so the field
    // clears regardless of what a caller's onClear does. Some callers' onClear
    // only resets results/suggestions and NOT the query state (e.g. StudioSearch's
    // resetResults), which previously left the field populated after clearing.
    // onClear then runs for any extra results/URL/state cleanup the caller needs.
    onChange?.('')
    onClear?.()
    setSuggestions([])
    setHighlightIndex(-1)
    setOpen(false)
    setGhostDismissed(false)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  function attachInputRef(node) {
    inputRef.current = node

    if (!externalInputRef) {
      return
    }

    if (typeof externalInputRef === 'function') {
      externalInputRef(node)
      return
    }

    externalInputRef.current = node
  }

  return (
    <div className={`search-autosuggest${hasClearAction ? ' has-clear-action' : ''}`}>
      <div className="search-autosuggest-input-wrap">
        {ghostSuggestion ? (
          <div className="search-autosuggest-ghost" aria-hidden="true">
            <span className="search-autosuggest-ghost-typed">{value}</span>
            <span className="search-autosuggest-ghost-completion">{ghostSuggestion.completion}</span>
          </div>
        ) : null}
        <input
          {...resolvedInputProps}
          ref={attachInputRef}
          type={inputType}
          role="combobox"
          aria-expanded={shouldShowDropdown}
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-activedescendant={
            shouldShowDropdown && highlightIndex >= 0 ? `${listboxId}-opt-${highlightIndex}` : undefined
          }
          value={value || ''}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onFocus={handleFocus}
          onBlur={handleBlur}
          onCompositionStart={() => setIsComposing(true)}
          onCompositionEnd={() => setIsComposing(false)}
          placeholder={placeholder}
          disabled={disabled}
          autoComplete="off"
        />
        {hasClearAction ? (
          <button
            type="button"
            className="search-autosuggest-clear"
            aria-label={clearLabel}
            // Prevent the input from blurring on press (which would fire the
            // blur close-timer first); the actual clear runs once on click so
            // keyboard activation (Enter/Space) works and it never double-fires.
            onPointerDown={(event) => event.preventDefault()}
            onClick={handleClear}
            disabled={disabled}
          >
            <span className="search-autosuggest-clear-glyph" aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {shouldShowDropdown ? (
        <ul
          id={listboxId}
          role="listbox"
          className="search-autosuggest-dropdown"
          aria-label="Search suggestions"
        >
          {suggestions.map((item, index) => {
            const isActive = index === highlightIndex
            return (
              <li
                id={`${listboxId}-opt-${index}`}
                key={`${item.phrase_text}-${item.source}-${index}`}
                role="option"
                aria-selected={isActive}
                className={`search-autosuggest-item${isActive ? ' is-active' : ''}`}
                onMouseDown={(event) => handleItemMouseDown(event, item)}
                onMouseEnter={() => setHighlightIndex(index)}
              >
                <span className="search-autosuggest-phrase">{item.phrase_text}</span>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}
