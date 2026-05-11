import { useState } from 'react'
import { ApiError } from '@/api/client'
import { useCreateCron } from '@/api/crons'
import type { Schema } from '@/api/types'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { CronForm } from '@/components/crons/CronForm'

type CronCreate = Schema<'CronCreate'>
type CronUpdate = Schema<'CronUpdate'>

export interface AddCronModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function AddCronModal({ open, onOpenChange }: AddCronModalProps) {
  const create = useCreateCron()
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (body: CronCreate | CronUpdate) => {
    setError(null)
    try {
      await create.mutateAsync(body as CronCreate)
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Unexpected error creating cron.')
      }
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add cron</DialogTitle>
          <DialogDescription>
            Manually register a cron job for monitoring. Auto-discovery lands in STAGE-002-003.
          </DialogDescription>
        </DialogHeader>
        <CronForm
          mode="create"
          onSubmit={handleSubmit}
          onCancel={() => onOpenChange(false)}
          errorMessage={error}
          isSubmitting={create.isPending}
        />
      </DialogContent>
    </Dialog>
  )
}
