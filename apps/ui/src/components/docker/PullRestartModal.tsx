import React, { useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useStartPullAndRestart } from '@/api/docker'
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

interface PullRestartModalProps {
  containerName: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onActionStarted: (actionId: number) => void
  /** Optional current/latest digest preview shown above the confirm input. */
  currentDigest?: string | null
  latestDigest?: string | null
  /** Optional command preview shown above the confirm input. */
  commandPreview?: string
  /** Label for the action button and dialog title. Defaults to 'Pull & Restart'. */
  actionLabel?: string
  /** Phrase user must type to confirm. Defaults to 'pull'. */
  confirmPhrase?: string
}

export function PullRestartModal({
  containerName,
  open,
  onOpenChange,
  onActionStarted,
  currentDigest,
  latestDigest,
  commandPreview,
  actionLabel = 'Pull & Restart',
  confirmPhrase = 'pull',
}: PullRestartModalProps) {
  const [inputPhrase, setInputPhrase] = useState('')
  const mutation = useStartPullAndRestart(containerName)

  const label = actionLabel
  const phrase = confirmPhrase

  React.useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setInputPhrase('')
    }
  }, [open])

  const handleSubmit = async () => {
    try {
      const result = await mutation.mutateAsync({ confirmPhrase: inputPhrase })
      toast.success(`${label} started for ${containerName}`)
      onActionStarted(result.action_id)
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 400) {
          toast.error(`Confirm phrase must be "${phrase}"`)
        } else if (err.status === 404) {
          toast.error(`Container not found: ${containerName}`)
        } else if (err.status === 403) {
          toast.error('Forbidden — your session lacks docker:write')
        } else if (err.status === 409) {
          toast.info(`${label} already in progress for ${containerName}`)
        } else {
          toast.error(err.message)
        }
      } else {
        toast.error(`${label} failed`)
      }
    }
  }

  const isConfirmed = inputPhrase.trim().toLowerCase() === phrase
  const submitDisabled = !isConfirmed || mutation.isPending

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {label} {containerName}
          </DialogTitle>
          <DialogDescription>
            This will run{' '}
            <code>
              {label === 'Rebuild & Restart' ? 'docker compose build' : 'docker compose pull'}
            </code>{' '}
            followed by <code>up -d</code> for the service. The container will restart.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {currentDigest && latestDigest && (
            <div className="rounded-md border border-border bg-card p-3 text-xs">
              <div className="text-muted-foreground">Current digest:</div>
              <div className="font-mono break-all">{currentDigest}</div>
              <div className="mt-2 text-muted-foreground">Latest digest:</div>
              <div className="font-mono break-all">{latestDigest}</div>
            </div>
          )}
          {commandPreview && (
            <div className="rounded-md border border-border bg-muted/40 p-3 text-xs">
              <div className="text-muted-foreground">Command preview:</div>
              <code className="font-mono break-all">{commandPreview}</code>
            </div>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="pull-confirm-phrase">
              Type <strong>{phrase}</strong> to confirm
            </Label>
            <Input
              id="pull-confirm-phrase"
              value={inputPhrase}
              onChange={(e) => setInputPhrase(e.currentTarget.value)}
              autoComplete="off"
              placeholder={phrase}
            />
          </div>
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => void handleSubmit()}
            disabled={submitDisabled}
          >
            {mutation.isPending ? 'Starting…' : label}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
