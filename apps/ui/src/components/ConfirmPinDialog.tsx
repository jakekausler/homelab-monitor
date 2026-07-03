import React, { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface ConfirmPinDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  /** Inline content only — rendered inside a <p> (DialogDescription). */
  body: React.ReactNode
  confirmLabel: string
  /** Receives the entered PIN string. Caller uses it as confirm_pin in the destructive-action mutation. */
  onConfirm: (pin: string) => void
  isPending: boolean
  errorMessage?: string | undefined
  /** When set, shows a live countdown and disables submit until reaches 0. */
  retryAfterSeconds?: number | undefined
}

export function ConfirmPinDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel,
  onConfirm,
  isPending,
  errorMessage,
  retryAfterSeconds,
}: ConfirmPinDialogProps) {
  const [pin, setPin] = useState('')
  const [countdown, setCountdown] = useState(retryAfterSeconds ?? 0)
  const [showError, setShowError] = useState(true)

  React.useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setPin('')
    }
  }, [open])

  const wasLockedRef = React.useRef(false)

  React.useEffect(() => {
    if (!retryAfterSeconds || retryAfterSeconds <= 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setCountdown(0)
      if (wasLockedRef.current) {
        // Transitioning from locked -> unlocked: clear stale PIN + suppress stale error once.
        // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
        setPin('')
        // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
        setShowError(false)
      }
      wasLockedRef.current = false
      return
    }
    wasLockedRef.current = true
    // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
    setCountdown(retryAfterSeconds)
    const t = window.setInterval(() => {
      setCountdown((c) => {
        if (c <= 1) {
          window.clearInterval(t)
          if (wasLockedRef.current) {
            setPin('')
            setShowError(false)
          }
          wasLockedRef.current = false
          return 0
        }
        return c - 1
      })
    }, 1000)
    return () => window.clearInterval(t)
  }, [retryAfterSeconds])

  React.useEffect(() => {
    if (errorMessage !== undefined && errorMessage !== '') {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setShowError(true)
    }
  }, [errorMessage])

  const isValidPin = /^[0-9]{4,12}$/.test(pin)
  const submitDisabled = !isValidPin || isPending || countdown > 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="confirm-pin-input">Enter your PIN (4-12 digits)</Label>
            <Input
              id="confirm-pin-input"
              type="password"
              inputMode="numeric"
              pattern="[0-9]*"
              autoComplete="off"
              maxLength={12}
              minLength={4}
              value={pin}
              onChange={(e) => setPin(e.target.value.replace(/[^0-9]/g, ''))}
              placeholder="4-12 digits"
              aria-label="PIN"
            />
          </div>
          <div className="space-y-1.5">
            {countdown > 0 && (
              <p role="alert" className="text-sm text-destructive">
                Too many wrong attempts. Please wait {countdown}s before trying again.
              </p>
            )}
            {countdown === 0 && showError && errorMessage !== undefined && errorMessage !== '' && (
              <p role="alert" className="text-sm text-destructive">
                {errorMessage}
              </p>
            )}
          </div>
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => onConfirm(pin)} disabled={submitDisabled}>
            {isPending ? 'Working…' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
