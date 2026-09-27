/*
 * NotifyCapture
 *
 * Email capture form that posts to POST /api/v1/public/subscribers.
 * Pitch copy locked by founder decision #6:
 *   "Leave an email to be notified when the next object is published"
 *
 * Slice D authors the subscriber backend. Until Slice D ships, the POST
 * 404s and we surface a graceful "coming soon" message rather than an
 * error — the landing scaffold remains visually complete during dev.
 *
 * On success the form is replaced by the thank-you state.
 *
 * Submit endpoint (per session plan §8 Slice D):
 *   POST /api/v1/public/subscribers  body: { email, customer_key? }
 */
import { useState } from 'react'

const SUBSCRIBE_ENDPOINT = '/api/v1/public/subscribers'

/**
 * Derive a coarse UA class for high-signal optimization later (per Slice D
 * data-minimization posture: NO full UA string is ever sent — fingerprinting
 * vector — only this 4-bucket enum). Read-only at submit time, no state.
 */
function deriveUserAgentClass() {
  if (typeof window === 'undefined' || !window.matchMedia) return 'other'
  if (window.matchMedia('(max-width: 599px)').matches) return 'mobile'
  if (window.matchMedia('(max-width: 1023px)').matches) return 'tablet'
  return 'desktop'
}

function deriveLanguage() {
  if (typeof navigator === 'undefined') return null
  const lang = navigator.language || navigator.userLanguage || null
  if (!lang) return null
  // Cap at 16 chars to match the schema's max_length; realistic BCP-47 tags
  // (e.g., "en-US", "zh-Hans-CN") fit comfortably under 16.
  return String(lang).slice(0, 16)
}

function deriveSourceUrl() {
  if (typeof window === 'undefined' || !window.location) return null
  // Cap at 2048 chars to match schema; production URLs should be far shorter.
  return String(window.location.href).slice(0, 2048)
}

export default function NotifyCapture() {
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState('idle') // idle | submitting | success | unavailable | invalid

  const handleSubmit = async (e) => {
    e.preventDefault()
    const trimmed = email.trim()
    if (!trimmed || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      setStatus('invalid')
      return
    }
    setStatus('submitting')
    try {
      const payload = {
        email: trimmed,
        source_url: deriveSourceUrl(),
        user_agent_class: deriveUserAgentClass(),
        language: deriveLanguage(),
      }
      const res = await fetch(SUBSCRIBE_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (res.status === 404) {
        // Slice D-3 not yet shipped on the server we hit — graceful degrade.
        setStatus('unavailable')
        return
      }
      if (res.status === 429) {
        // Rate-limited (5/min/IP). Surface the retry guidance in the
        // status pane rather than silently failing.
        setStatus('rate_limited')
        return
      }
      if (!res.ok) throw new Error(`subscribe ${res.status}`)
      setStatus('success')
    } catch (err) {
      // Network or server error → treat as unavailable rather than scaring
      // the visitor with a stack trace.
      setStatus('unavailable')
    }
  }

  if (status === 'success') {
    return (
      <div className="notify-capture" role="status" aria-live="polite">
        <p className="notify-capture__status notify-capture__status--success">
          We&rsquo;ll email you when the next object opens. You can unsubscribe any time.
        </p>
      </div>
    )
  }

  if (status === 'unavailable') {
    return (
      <div className="notify-capture" role="status" aria-live="polite">
        <p className="notify-capture__pitch">
          Leave an email to be notified when the next object is published.
        </p>
        <p className="notify-capture__status">
          Notifications coming soon — check back shortly.
        </p>
      </div>
    )
  }

  if (status === 'rate_limited') {
    return (
      <div className="notify-capture" role="status" aria-live="polite">
        <p className="notify-capture__pitch">
          Leave an email to be notified when the next object is published.
        </p>
        <p className="notify-capture__status">
          Too many subscribe attempts from this network — please try again in a minute.
        </p>
      </div>
    )
  }

  return (
    <div className="notify-capture">
      <p className="notify-capture__pitch">
        Leave an email to be notified when the next object is published.
      </p>
      <form className="notify-capture__form" onSubmit={handleSubmit} noValidate>
        <label className="visually-hidden" htmlFor="notify-email">
          Email address
        </label>
        <input
          id="notify-email"
          className="notify-capture__input"
          type="email"
          inputMode="email"
          autoComplete="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value)
            if (status === 'invalid') setStatus('idle')
          }}
          required
          aria-invalid={status === 'invalid' ? 'true' : 'false'}
          aria-describedby={status === 'invalid' ? 'notify-error' : undefined}
        />
        <button
          type="submit"
          className="notify-capture__button"
          disabled={status === 'submitting'}
        >
          {status === 'submitting' ? 'Sending…' : 'Notify me'}
        </button>
      </form>
      {status === 'invalid' ? (
        <p id="notify-error" className="notify-capture__status notify-capture__status--error">
          Please enter a valid email address.
        </p>
      ) : null}
    </div>
  )
}
