import React, { useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useInstallWrapper } from '@/api/crons'
import type { Schema } from '@/api/types'
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

interface InstallHeartbeatModalProps {
  fingerprint: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

type InstallResponse = Schema<'InstallWrapperPreview'> | Schema<'InstallWrapperResult'>

function isPreview(data: InstallResponse): data is Schema<'InstallWrapperPreview'> {
  return 'wrapper_content' in data && 'crontab_diff' in data
}

export function InstallHeartbeatModal({
  fingerprint,
  open,
  onOpenChange,
}: InstallHeartbeatModalProps) {
  const [isConfirmed, setIsConfirmed] = useState(false)
  const mutation = useInstallWrapper(fingerprint)

  // Fire dry-run on open
  const [previewData, setPreviewData] = useState<InstallResponse | null>(null)
  const [hasLoadedPreview, setHasLoadedPreview] = useState(false)

  // Reset modal state when it opens, so a re-open starts fresh.
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
          // Mark the preview load as attempted so the effect does not loop.
          // The mutation's own error state drives the visible error message.
          setHasLoadedPreview(true)
        })
    }
  }, [open, hasLoadedPreview, previewData, mutation])

  const handleInstall = async () => {
    try {
      await mutation.mutateAsync({ confirm: true })
      toast.success('Heartbeat wrapper installed')
      onOpenChange(false)
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Install failed'
      if (err instanceof ApiError) {
        if (err.status === 409) {
          toast.error('Line not found or already wrapped')
        } else if (err.status === 400) {
          toast.error('Cannot install on remote host')
        } else {
          toast.error(msg)
        }
      } else {
        toast.error(msg)
      }
    }
  }

  const isLoading = mutation.isPending && !previewData
  const isPreviewLoading = mutation.isPending && previewData !== null && !isConfirmed
  const error = mutation.error

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Install heartbeat wrapper</DialogTitle>
          <DialogDescription>
            Replace ad-hoc heartbeats with a managed wrapper script on the host.
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
                : 'Failed to load the install preview. Please try again.'}
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
            {/* Error banner if present */}
            {error && (
              <div
                role="alert"
                className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-900 dark:text-red-200"
              >
                {error.message}
              </div>
            )}

            {/* Crontab diff */}
            <div className="min-w-0">
              <h3 className="mb-2 text-sm font-medium">Crontab diff</h3>
              <div className="min-w-0 rounded-md bg-muted p-3 font-mono text-xs">
                <div className="mb-1 text-muted-foreground">
                  File: {previewData.crontab_diff.source_path}
                </div>
                <div className="mb-2 flex items-start gap-2">
                  <span className="text-red-600 dark:text-red-400">-</span>
                  <code className="break-all text-red-600 line-through dark:text-red-400">
                    {previewData.crontab_diff.old_line}
                  </code>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-green-600 dark:text-green-400">+</span>
                  <code className="break-all text-green-600 dark:text-green-400">
                    {previewData.crontab_diff.new_line}
                  </code>
                </div>
              </div>
            </div>

            {/* Wrapper script preview */}
            <div className="min-w-0">
              <h3 className="mb-2 text-sm font-medium">Wrapper script</h3>
              <pre className="max-h-48 min-w-0 max-w-full overflow-auto rounded-md bg-muted p-3 text-xs">
                <code>{previewData.wrapper_content}</code>
              </pre>
            </div>

            {/* Confirmation checkbox */}
            <div className="flex items-center gap-2">
              <Input
                id="confirm-wrapper"
                type="checkbox"
                checked={isConfirmed}
                onChange={(e) => setIsConfirmed(e.currentTarget.checked)}
                className="h-4 w-4"
              />
              <Label htmlFor="confirm-wrapper" className="text-sm text-muted-foreground">
                I understand this will modify my crontab on the host.
              </Label>
            </div>

            {/* Action buttons */}
            <DialogFooter className="gap-2 sm:gap-0">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                onClick={() => void handleInstall()}
                disabled={!isConfirmed || isPreviewLoading}
              >
                {isPreviewLoading ? 'Installing…' : 'Install'}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
