import React, { useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useUninstallWrapper, type UninstallWrapperResponse } from '@/api/crons'
import type { Schema } from '@/api/types'
import { CrontabLineDiff } from '@/components/crons/CrontabLineDiff'
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

interface RemoveHeartbeatModalProps {
  fingerprint: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

function isPreview(data: UninstallWrapperResponse): data is Schema<'UninstallWrapperPreview'> {
  return 'crontab_diff' in data
}

export function RemoveHeartbeatModal({
  fingerprint,
  open,
  onOpenChange,
}: RemoveHeartbeatModalProps) {
  const [isConfirmed, setIsConfirmed] = useState(false)
  const mutation = useUninstallWrapper(fingerprint)

  const [previewData, setPreviewData] = useState<UninstallWrapperResponse | null>(null)
  const [hasLoadedPreview, setHasLoadedPreview] = useState(false)

  React.useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setIsConfirmed(false)
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setPreviewData(null)
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setHasLoadedPreview(false)
    }
  }, [open])

  React.useEffect(() => {
    if (open && !hasLoadedPreview && !previewData) {
      void mutation
        .mutateAsync({ confirm: false })
        .then((data) => {
          setPreviewData(data)
          setHasLoadedPreview(true)
        })
        .catch(() => {
          setHasLoadedPreview(true)
        })
    }
  }, [open, hasLoadedPreview, previewData, mutation])

  const handleRemove = async () => {
    try {
      await mutation.mutateAsync({ confirm: true })
      toast.success('Heartbeat wrapper removed')
      onOpenChange(false)
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Remove failed'
      if (err instanceof ApiError) {
        if (err.status === 409) {
          toast.error('Line not found or not wrapped')
        } else if (err.status === 400) {
          toast.error('Cannot remove on remote host')
        } else {
          toast.error(msg)
        }
      } else {
        toast.error(msg)
      }
    }
  }

  const isLoading = mutation.isPending && !previewData
  const isConfirmLoading = mutation.isPending && isConfirmed
  const error = mutation.error

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Remove heartbeat wrapper</DialogTitle>
          <DialogDescription>
            Strip the managed wrapper from this cron's crontab line. The shared wrapper script and
            token file are left in place.
          </DialogDescription>
        </DialogHeader>

        {isLoading && <p className="text-muted-foreground">Loading preview…</p>}

        {hasLoadedPreview && !previewData && (
          <div className="space-y-4">
            <div
              role="alert"
              className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-900 dark:text-red-200"
            >
              {error instanceof ApiError
                ? error.message
                : 'Failed to load the removal preview. Please try again.'}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Close
              </Button>
            </DialogFooter>
          </div>
        )}

        {previewData && isPreview(previewData) && (
          <div className="min-w-0 space-y-4">
            {error && (
              <div
                role="alert"
                className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-900 dark:text-red-200"
              >
                {error.message}
              </div>
            )}

            <CrontabLineDiff
              sourcePath={previewData.crontab_diff.source_path}
              oldLine={previewData.crontab_diff.old_line}
              newLine={previewData.crontab_diff.new_line}
            />

            <div className="flex items-center gap-2">
              <Input
                id="confirm-unwrap"
                type="checkbox"
                checked={isConfirmed}
                onChange={(e) => setIsConfirmed(e.currentTarget.checked)}
                className="h-4 w-4"
              />
              <Label htmlFor="confirm-unwrap" className="text-sm text-muted-foreground">
                I understand this will modify my crontab on the host.
              </Label>
            </div>

            <DialogFooter className="gap-2 sm:gap-0">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => void handleRemove()}
                disabled={!isConfirmed || mutation.isPending}
              >
                {isConfirmLoading ? 'Removing…' : 'Remove'}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
