import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useCreateProbeTarget } from '@/api/docker'
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
import { Select } from '@/components/ui/select'

type ProbeKind = 'http' | 'tcp' | 'exec' | 'metrics'

const PROBE_NAME_REGEX = /^[a-zA-Z0-9_-]{1,64}$/

interface AddProbeModalProps {
  containerName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function AddProbeModal({ containerName, open, onOpenChange }: AddProbeModalProps) {
  const [kind, setKind] = useState<ProbeKind>('http')
  const [name, setName] = useState('')
  const [targetValue, setTargetValue] = useState('')
  const [intervalSeconds, setIntervalSeconds] = useState(60)
  const [timeoutSeconds, setTimeoutSeconds] = useState(10)
  const mutation = useCreateProbeTarget()

  useEffect(() => {
    if (open) {
      // eslint-disable-next-line @eslint-react/set-state-in-effect, react-hooks/set-state-in-effect
      setKind('http')
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setName('')
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setTargetValue('')
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setIntervalSeconds(60)
      // eslint-disable-next-line @eslint-react/set-state-in-effect
      setTimeoutSeconds(10)
    }
  }, [open])

  const nameError = !PROBE_NAME_REGEX.test(name)
    ? 'Name must match ^[a-zA-Z0-9_-]{1,64}$'
    : undefined
  const targetError = targetValue.trim().length === 0 ? 'Target is required' : undefined
  const intervalError =
    !Number.isInteger(intervalSeconds) || intervalSeconds < 1 || intervalSeconds > 3600
      ? 'Interval must be 1-3600'
      : undefined
  const timeoutError =
    !Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 300
      ? 'Timeout must be 1-300'
      : undefined
  const hasErrors = Boolean(nameError || targetError || intervalError || timeoutError)

  const handleSubmit = async () => {
    if (hasErrors) return
    try {
      await mutation.mutateAsync({
        body: {
          container_name: containerName,
          kind,
          name,
          target_value: targetValue,
          interval_seconds: intervalSeconds,
          timeout_seconds: timeoutSeconds,
        },
      })
      toast.success(`Added probe ${kind}.${name}`)
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError) {
        toast.error(err.message || 'Add failed')
      } else {
        toast.error('Add failed')
      }
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-md"
        data-testid="add-probe-modal"
      >
        <DialogHeader>
          <DialogTitle>Add probe to {containerName}</DialogTitle>
          <DialogDescription>Define a single probe targeting this container.</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="add-probe-kind">Kind</Label>
            <Select
              id="add-probe-kind"
              value={kind}
              onChange={(e) => setKind(e.currentTarget.value as ProbeKind)}
              data-testid="add-probe-kind"
            >
              <option value="http">http</option>
              <option value="tcp">tcp</option>
              <option value="exec">exec</option>
              <option value="metrics">metrics</option>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="add-probe-name">Name</Label>
            <Input
              id="add-probe-name"
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              data-testid="add-probe-name"
              placeholder="default"
            />
            {nameError && <p className="text-xs text-destructive">{nameError}</p>}
          </div>
          <div className="space-y-1">
            <Label htmlFor="add-probe-target">Target value</Label>
            <Input
              id="add-probe-target"
              value={targetValue}
              onChange={(e) => setTargetValue(e.currentTarget.value)}
              data-testid="add-probe-target"
              placeholder={
                kind === 'http' || kind === 'metrics'
                  ? 'https://example.com/health'
                  : kind === 'tcp'
                    ? 'tcp://host.docker.internal:8080'
                    : 'sh command to run'
              }
            />
            {targetError && <p className="text-xs text-destructive">{targetError}</p>}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label htmlFor="add-probe-interval">Interval (s)</Label>
              <Input
                id="add-probe-interval"
                type="number"
                min={1}
                max={3600}
                value={intervalSeconds}
                onChange={(e) => setIntervalSeconds(Number(e.currentTarget.value))}
                data-testid="add-probe-interval"
              />
              {intervalError && <p className="text-xs text-destructive">{intervalError}</p>}
            </div>
            <div className="space-y-1">
              <Label htmlFor="add-probe-timeout">Timeout (s)</Label>
              <Input
                id="add-probe-timeout"
                type="number"
                min={1}
                max={300}
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(Number(e.currentTarget.value))}
                data-testid="add-probe-timeout"
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
            data-testid="add-probe-submit"
          >
            {mutation.isPending ? 'Saving…' : 'Add probe'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
