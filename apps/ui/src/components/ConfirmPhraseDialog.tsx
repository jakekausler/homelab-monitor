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

// NOTE: PullRestartModal could adopt this later (deferred, non-blocking).

interface ConfirmPhraseDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  /** Inline content only — rendered inside a <p> (DialogDescription). */
  body: React.ReactNode
  expectedPhrase: string
  confirmLabel: string
  onConfirm: () => void
  isPending: boolean
  errorMessage?: string
}

export function ConfirmPhraseDialog({
  open,
  onOpenChange,
  title,
  body,
  expectedPhrase,
  confirmLabel,
  onConfirm,
  isPending,
  errorMessage,
}: ConfirmPhraseDialogProps) {
  const [inputPhrase, setInputPhrase] = useState('')

  React.useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setInputPhrase('')
    }
  }, [open])

  const isConfirmed = inputPhrase.trim().toLowerCase() === expectedPhrase.trim().toLowerCase()
  const submitDisabled = !isConfirmed || isPending

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="confirm-phrase-input">
              Type <strong>{expectedPhrase}</strong> to confirm
            </Label>
            <Input
              id="confirm-phrase-input"
              value={inputPhrase}
              onChange={(e) => setInputPhrase(e.currentTarget.value)}
              autoComplete="off"
              placeholder={expectedPhrase}
            />
          </div>
          {errorMessage !== undefined && errorMessage !== '' && (
            <p role="alert" className="text-sm text-destructive">
              {errorMessage}
            </p>
          )}
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={submitDisabled}>
            {isPending ? 'Working…' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
