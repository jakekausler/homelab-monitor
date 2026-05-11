import { useState } from 'react'

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

export interface ConfirmDeleteModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  cronName: string
  onConfirm: () => Promise<void> | void
  isDeleting?: boolean
  errorMessage?: string | null
}

export function ConfirmDeleteModal({
  open,
  onOpenChange,
  cronName,
  onConfirm,
  isDeleting,
  errorMessage,
}: ConfirmDeleteModalProps) {
  const [typed, setTyped] = useState('')
  const matches = typed === cronName

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setTyped('')
        onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Soft-delete cron?</DialogTitle>
          <DialogDescription>
            This archives the cron. Heartbeats from this cron will return 404 until restored. Type
            the cron name to confirm.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="confirm-name">
            Type <span className="font-mono">{cronName}</span> to confirm
          </Label>
          <Input
            id="confirm-name"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
          />
          {errorMessage !== null && errorMessage !== undefined && (
            <p role="alert" className="text-sm text-red-600">
              {errorMessage}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches || isDeleting}
            onClick={() => {
              void onConfirm()
            }}
          >
            {isDeleting ? 'Deleting…' : 'Soft-delete'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
