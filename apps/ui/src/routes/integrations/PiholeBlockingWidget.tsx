import { useState, type JSX } from 'react'

import { useBlockingMutation, usePiholeOverview } from '@/api/pihole'
import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { Select } from '@/components/ui/select'

import { useReenableCountdown } from './useReenableCountdown'

type BlockingAction = 'enable' | 'disable'

type TimerPreset = '30' | '300' | '3600' | 'indefinite'

const TIMER_PRESETS: { value: TimerPreset; label: string }[] = [
  { value: '30', label: '30s' },
  { value: '300', label: '5m' },
  { value: '3600', label: '1h' },
  { value: 'indefinite', label: 'Indefinite' },
]

function formatMSS(total: number | null): string {
  if (total === null) return '—'
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function PiholeBlockingWidget(): JSX.Element {
  const result = usePiholeOverview()
  const mutation = useBlockingMutation()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [pendingAction, setPendingAction] = useState<BlockingAction | null>(null)
  const [preset, setPreset] = useState<TimerPreset>('indefinite')
  const [errorMessage, setErrorMessage] = useState<string>('')

  const blockingEnabled = result.data?.blocking_enabled
  const blockingTimer = result.data?.blocking_timer_seconds
  const remaining = useReenableCountdown(blockingEnabled === false ? blockingTimer : null)

  const openDialog = (action: BlockingAction): void => {
    setPendingAction(action)
    setErrorMessage('')
    setDialogOpen(true)
  }

  const handleConfirm = (): void => {
    if (pendingAction === null) return
    const action = pendingAction
    const body: { action: BlockingAction; confirm_phrase: string; timer?: number } = {
      action,
      confirm_phrase: action,
    }
    if (action === 'disable' && preset !== 'indefinite') {
      body.timer = Number(preset)
    }
    setErrorMessage('')
    mutation.mutate(body, {
      onSuccess: () => {
        setDialogOpen(false)
      },
      onError: (err) => {
        if (err instanceof ApiError && err.status === 400) {
          setErrorMessage(`Confirm phrase must be "${action}"`)
        } else {
          setErrorMessage(err instanceof Error ? err.message : 'Request failed')
        }
      },
    })
  }

  return (
    <div data-testid="pihole-blocking-widget" className="space-y-3 text-sm">
      {result.isPending && <p className="text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Pi-hole metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}

      {result.data && (
        <>
          {blockingEnabled === true && (
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <p className="text-emerald-700 dark:text-emerald-300">Blocking is on.</p>
              <div className="flex items-end gap-2">
                <div className="space-y-1">
                  <label
                    htmlFor="pihole-disable-timer"
                    className="block text-xs text-muted-foreground"
                  >
                    Disable for
                  </label>
                  <Select
                    id="pihole-disable-timer"
                    data-testid="pihole-disable-timer"
                    value={preset}
                    onChange={(e) => setPreset(e.currentTarget.value as TimerPreset)}
                    className="w-32"
                  >
                    {TIMER_PRESETS.map((p) => (
                      <option key={p.value} value={p.value}>
                        {p.label}
                      </option>
                    ))}
                  </Select>
                </div>
                <Button variant="destructive" onClick={() => openDialog('disable')}>
                  Disable blocking
                </Button>
              </div>
            </div>
          )}

          {blockingEnabled === false && (
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              <p className="text-muted-foreground">
                Blocking is off
                {blockingTimer != null && blockingTimer > 0 && (
                  <>
                    {' · re-enables in '}
                    <span className="tabular-nums">{formatMSS(remaining)}</span>
                  </>
                )}
                .
              </p>
              <Button variant="default" onClick={() => openDialog('enable')}>
                Enable blocking
              </Button>
            </div>
          )}

          {blockingEnabled === null && (
            <div className="flex items-center gap-3">
              <p className="text-muted-foreground">Blocking state unknown (—)</p>
              <Button variant="default" disabled>
                Enable blocking
              </Button>
            </div>
          )}
        </>
      )}

      <ConfirmPhraseDialog
        open={dialogOpen}
        onOpenChange={(o) => {
          setDialogOpen(o)
          if (!o) setErrorMessage('')
        }}
        title={pendingAction === 'disable' ? 'Disable Pi-hole blocking' : 'Enable Pi-hole blocking'}
        body={
          pendingAction === 'disable'
            ? 'This stops DNS-level ad/tracker blocking for the whole network.'
            : 'This resumes DNS-level ad/tracker blocking for the whole network.'
        }
        expectedPhrase={pendingAction ?? 'confirm'}
        confirmLabel={pendingAction === 'disable' ? 'Disable blocking' : 'Enable blocking'}
        onConfirm={handleConfirm}
        isPending={mutation.isPending}
        errorMessage={errorMessage}
      />
    </div>
  )
}
