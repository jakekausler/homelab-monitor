import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useUpdateProbeTarget } from '@/api/docker'
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
import type { Schema } from '@/api/types'

type ProbeRow = Schema<'ProbeRow'>

interface EditProbeModalProps {
  probe: ProbeRow
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EditProbeModal({ probe, open, onOpenChange }: EditProbeModalProps) {
  const [targetValue, setTargetValue] = useState(probe.target_value)
  const [intervalSeconds, setIntervalSeconds] = useState(probe.interval_seconds)
  const [timeoutSeconds, setTimeoutSeconds] = useState(probe.timeout_seconds)
  const mutation = useUpdateProbeTarget()

  useEffect(() => {
    if (open) {
      // eslint-disable-next-line @eslint-react/set-state-in-effect, react-hooks/set-state-in-effect
      setTargetValue(probe.target_value)
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setIntervalSeconds(probe.interval_seconds)
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setTimeoutSeconds(probe.timeout_seconds)
    }
  }, [open, probe.target_value, probe.interval_seconds, probe.timeout_seconds])

  const targetError = targetValue.trim().length === 0 ? 'Target is required' : undefined
  const intervalError =
    !Number.isInteger(intervalSeconds) || intervalSeconds < 1 || intervalSeconds > 3600
      ? 'Interval must be 1-3600'
      : undefined
  const timeoutError =
    !Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 300
      ? 'Timeout must be 1-300'
      : undefined
  const hasErrors = Boolean(targetError || intervalError || timeoutError)

  const handleSubmit = async () => {
    if (hasErrors) return
    try {
      await mutation.mutateAsync({
        probeId: probe.id,
        containerName: probe.container_name,
        body: {
          target_value: targetValue,
          interval_seconds: intervalSeconds,
          timeout_seconds: timeoutSeconds,
        },
      })
      toast.success(`Updated ${probe.kind}.${probe.name}`)
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError) {
        toast.error(err.message || 'Update failed')
      } else {
        toast.error('Update failed')
      }
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-md"
        data-testid="edit-probe-modal"
      >
        <DialogHeader>
          <DialogTitle>
            Edit {probe.kind}.{probe.name} on {probe.container_name}
          </DialogTitle>
          <DialogDescription>
            Kind and name are immutable. Update target, interval, or timeout.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label>Kind</Label>
              <p
                className="rounded border border-border bg-muted px-2 py-1 text-xs"
                data-testid="edit-probe-kind-readonly"
              >
                {probe.kind}
              </p>
            </div>
            <div className="space-y-1">
              <Label>Name</Label>
              <p
                className="rounded border border-border bg-muted px-2 py-1 text-xs"
                data-testid="edit-probe-name-readonly"
              >
                {probe.name}
              </p>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="edit-probe-target">Target value</Label>
            <Input
              id="edit-probe-target"
              value={targetValue}
              onChange={(e) => setTargetValue(e.currentTarget.value)}
              data-testid="edit-probe-target"
            />
            {targetError && <p className="text-xs text-destructive">{targetError}</p>}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label htmlFor="edit-probe-interval">Interval (s)</Label>
              <Input
                id="edit-probe-interval"
                type="number"
                min={1}
                max={3600}
                value={intervalSeconds}
                onChange={(e) => setIntervalSeconds(Number(e.currentTarget.value))}
                data-testid="edit-probe-interval"
              />
              {intervalError && <p className="text-xs text-destructive">{intervalError}</p>}
            </div>
            <div className="space-y-1">
              <Label htmlFor="edit-probe-timeout">Timeout (s)</Label>
              <Input
                id="edit-probe-timeout"
                type="number"
                min={1}
                max={300}
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(Number(e.currentTarget.value))}
                data-testid="edit-probe-timeout"
              />
              {timeoutError && <p className="text-xs text-destructive">{timeoutError}</p>}
            </div>
          </div>
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={hasErrors || mutation.isPending}
            data-testid="edit-probe-submit"
          >
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
