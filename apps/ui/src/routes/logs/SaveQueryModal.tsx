import { useState } from 'react'

import { ApiError } from '@/api/client'
import {
  useCreateSavedLogQuery,
  type SavedQuery,
  type SaveQueryCreateRequest,
} from '@/api/savedLogQueries'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'

interface SaveQueryModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Pre-serialized create-request payload built by the page from current state. */
  buildPayload: (name: string) => SaveQueryCreateRequest
  /** Called after a successful save (e.g. to close + toast). */
  onSaved?: (saved: SavedQuery) => void
}

export function SaveQueryModal({ open, onOpenChange, buildPayload, onSaved }: SaveQueryModalProps) {
  // Reset state when modal is closed to ensure clean state on next open
  const [name, setName] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const createMut = useCreateSavedLogQuery()

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()

    const trimmedName = name.trim()
    if (!trimmedName) {
      setLocalError('Name is required.')
      return
    }

    const payload = buildPayload(trimmedName)
    createMut.mutate(payload, {
      onSuccess: (saved) => {
        onOpenChange(false)
        onSaved?.(saved)
      },
      onError: (err: ApiError) => {
        if (err.status === 409) {
          setLocalError('A query with that name already exists.')
        } else {
          setLocalError(err.message)
        }
      },
    })
  }

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      // Reset state when closing
      setName('')
      setLocalError(null)
    }
    onOpenChange(nextOpen)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="save-query-modal">
        <DialogTitle>Save Query</DialogTitle>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="save-query-name-input" className="text-sm font-medium">
              Query Name
            </label>
            <input
              id="save-query-name-input"
              type="text"
              data-testid="save-query-name"
              value={name}
              onChange={(e) => {
                setName(e.target.value)
                setLocalError(null)
              }}
              placeholder="Enter query name…"
              className="mt-1 flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
          {localError && (
            <p role="alert" data-testid="save-query-error" className="text-sm text-red-600">
              {localError}
            </p>
          )}
          <div className="flex gap-2 justify-end">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" data-testid="save-query-submit" disabled={createMut.isPending}>
              {createMut.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
