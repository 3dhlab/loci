import { useEffect, useRef } from 'react'

const CONFIRM_DIALOG_BUILD_MARKER = 'cd-native-dialog-v5'

export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  confirmBusy = false,
  onConfirm,
  onCancel
}) {
  const dialogRef = useRef(null)
  const cancelButtonRef = useRef(null)
  const confirmButtonRef = useRef(null)
  const onCancelRef = useRef(onCancel)
  const confirmBusyRef = useRef(confirmBusy)

  useEffect(() => {
    onCancelRef.current = onCancel
  })
  useEffect(() => {
    confirmBusyRef.current = confirmBusy
  })

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return undefined

    if (open && !dialog.open) {
      try {
        dialog.showModal()
      } catch {
        // ignore — already open or unsupported
      }
      cancelButtonRef.current?.focus()
    } else if (!open && dialog.open) {
      dialog.close()
    }

    function handleCancelEvent(event) {
      event.preventDefault()
      if (!confirmBusyRef.current) {
        onCancelRef.current?.()
      }
    }

    function handleKeyDown(event) {
      if (event.key !== 'Tab') return
      const focusables = [cancelButtonRef.current, confirmButtonRef.current].filter(
        (el) => el && !el.disabled
      )
      if (focusables.length === 0) {
        event.preventDefault()
        return
      }
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement
      if (event.shiftKey) {
        if (active === first || !focusables.includes(active)) {
          event.preventDefault()
          last.focus()
        }
      } else {
        if (active === last || !focusables.includes(active)) {
          event.preventDefault()
          first.focus()
        }
      }
    }

    dialog.addEventListener('cancel', handleCancelEvent)
    dialog.addEventListener('keydown', handleKeyDown)
    return () => {
      dialog.removeEventListener('cancel', handleCancelEvent)
      dialog.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  function handleBackdropClick(event) {
    if (event.target === dialogRef.current && !confirmBusy) {
      onCancelRef.current?.()
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="confirm-dialog"
      aria-labelledby="confirm-dialog-title"
      data-confirm-build={CONFIRM_DIALOG_BUILD_MARKER}
      onClick={handleBackdropClick}
    >
      <div className="overlay-card confirm-dialog-card">
        <strong id="confirm-dialog-title">{title}</strong>
        <p className="muted confirm-dialog-message">{message}</p>
        <div className="confirm-dialog-actions">
          <button
            type="button"
            className="ghost"
            ref={cancelButtonRef}
            onClick={onCancel}
            disabled={confirmBusy}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            ref={confirmButtonRef}
            onClick={onConfirm}
            disabled={confirmBusy}
          >
            {confirmBusy ? 'Working...' : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  )
}
