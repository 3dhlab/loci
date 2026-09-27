/*
 * SignInAffordance
 *
 * Ghost-link "Sign in" with inline disclosure panel (not a modal).
 * Copy locked by founder decision #14:
 *   "Hosted authoring access is coming. Researchers with an existing
 *    local instance continue to work at their private installation."
 *
 * Dismissal per founder decision #9: Esc key + outside-click both active.
 *
 * aria-expanded tracks disclosure state; aria-controls points to the panel.
 */
import { useEffect, useId, useRef, useState } from 'react'

const DISCLOSURE_COPY =
  'Hosted authoring access is coming. Researchers with an existing local instance continue to work at their private installation.'

export default function SignInAffordance() {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef(null)
  const panelRef = useRef(null)
  const panelId = useId()

  // Esc-to-dismiss + outside-click-to-dismiss
  useEffect(() => {
    if (!open) return undefined

    const handleKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    const handleClick = (e) => {
      if (
        panelRef.current &&
        !panelRef.current.contains(e.target) &&
        triggerRef.current &&
        !triggerRef.current.contains(e.target)
      ) {
        setOpen(false)
      }
    }

    document.addEventListener('keydown', handleKey)
    document.addEventListener('mousedown', handleClick)
    return () => {
      document.removeEventListener('keydown', handleKey)
      document.removeEventListener('mousedown', handleClick)
    }
  }, [open])

  return (
    <div className="signin-affordance">
      <button
        ref={triggerRef}
        type="button"
        className="signin-affordance__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((prev) => !prev)}
      >
        Sign in
      </button>
      {open ? (
        <div ref={panelRef} id={panelId} className="signin-affordance__panel" role="dialog" aria-label="Sign in details">
          {DISCLOSURE_COPY}
        </div>
      ) : null}
    </div>
  )
}
