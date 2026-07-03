import { useState } from 'react'
import type { JSX } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'
import { useTriggerRunbook, type Runbook } from '@/api/runbooks'
import { toast } from 'sonner'

interface RunFixDialogProps {
  runbook: Runbook
  open: boolean
  onOpenChange: (open: boolean) => void
}

const DENIAL_REASON_LABELS: Record<string, string> = {
  kill_switch: 'Global kill-switch is engaged',
  runbook_disabled: 'This runbook is disabled',
  rate_limit: 'Rate limit exceeded for this runbook',
  cooldown: 'Runbook is in cooldown',
  already_running: 'This runbook is already running',
  dry_run_required_for_risky: 'This runbook is risky and must be dry-run + approved',
  claim_error: 'Internal error while claiming the runbook; please retry.',
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  const idx = trimmed.lastIndexOf('/')
  return idx === -1 ? trimmed : trimmed.slice(idx + 1)
}

export function RunFixDialog({ runbook, open, onOpenChange }: RunFixDialogProps): JSX.Element {
  const [mode, setMode] = useState<'dry_run' | 'real'>('dry_run')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const trigger = useTriggerRunbook()
  const runbookName = basename(runbook.path)
  const isRisky = runbook.risk_tag === 'risky'
  const realDisabled = isRisky

  const handleSubmit = (): void => {
    if (mode === 'real') {
      setConfirmOpen(true)
      return
    }
    doTrigger()
  }

  const doTrigger = (): void => {
    trigger.mutate(
      { id: runbook.id, mode },
      {
        onSuccess: (data) => {
          if (data.outcome === 'dry_run_stored') {
            toast.success(`Dry run stashed for approval (${data.approval_id ?? 'no id'})`)
          } else if (data.outcome === 'ran') {
            toast.success(`Real run started (${data.run_id ?? 'no id'})`)
          }
          setConfirmOpen(false)
          onOpenChange(false)
        },
        onError: (err) => {
          const apiErr = err
          const code = apiErr?.code
          const label = (code && DENIAL_REASON_LABELS[code]) ?? apiErr?.message ?? 'Trigger failed'
          toast.error(label)
          setConfirmOpen(false)
        },
      },
    )
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg"
          data-testid="run-fix-dialog"
        >
          <DialogHeader>
            <DialogTitle>Run fix: {runbookName}</DialogTitle>
            <DialogDescription>
              Choose how to execute this runbook. Risky runbooks must go through a dry-run +
              approval flow.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Mode</legend>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="run-fix-mode"
                  value="dry_run"
                  checked={mode === 'dry_run'}
                  onChange={() => setMode('dry_run')}
                  data-testid="run-fix-mode-dry"
                />
                Dry run (plan only; queues for approval)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="run-fix-mode"
                  value="real"
                  checked={mode === 'real'}
                  disabled={realDisabled}
                  onChange={() => setMode('real')}
                  data-testid="run-fix-mode-real"
                />
                <div className="flex flex-col gap-0.5">
                  <span>Real (execute now)</span>
                  {realDisabled ? (
                    <span className="text-xs text-muted-foreground">
                      Disabled: risky runbook requires approval flow
                    </span>
                  ) : null}
                </div>
              </label>
            </fieldset>
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSubmit}
              disabled={trigger.isPending}
              data-testid="run-fix-submit"
            >
              {mode === 'real' ? 'Run for real' : 'Store dry run'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmPhraseDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Confirm real run of ${runbookName}`}
        body={
          <>
            <p>
              You are about to <strong>execute</strong> the runbook <code>{runbookName}</code>{' '}
              against production.
            </p>
            <p>
              Type <code>{runbookName}</code> below to confirm.
            </p>
          </>
        }
        expectedPhrase={runbookName}
        confirmLabel="Run for real"
        onConfirm={doTrigger}
        isPending={trigger.isPending}
      />
    </>
  )
}
