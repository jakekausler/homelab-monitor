import { useState, type JSX } from 'react'
import { toast } from 'sonner'

import { useAdlists, useGravityUpdateMutation } from '@/api/pihole'
import { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { formatAge } from '@/lib/relativeTime'

import { adlistStatusToBadgeVariant } from './piholeStatus'

type AdlistRow = Schema<'PiholeAdlistRow'>

export function PiholeGravityWidget(): JSX.Element {
  const result = useAdlists()
  const mutation = useGravityUpdateMutation()

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [resultOpen, setResultOpen] = useState(false)
  const [logTail, setLogTail] = useState<string[]>([])
  const [resultSuccess, setResultSuccess] = useState(true)

  const handleConfirm = (): void => {
    setErrorMessage('')
    mutation.mutate(
      { confirm_phrase: 'update' },
      {
        onSuccess: (data) => {
          setConfirmOpen(false)
          setLogTail(data.log_tail)
          setResultSuccess(data.success)
          if (!data.success) {
            toast.error('Gravity update reported failure — see log output')
          }
          setResultOpen(true)
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 400) {
            setErrorMessage('Confirm phrase must be "update"')
          } else {
            const msg = err instanceof Error ? err.message : 'Gravity update failed'
            setErrorMessage(msg)
            toast.error(`Gravity update failed: ${msg}`)
          }
        },
      },
    )
  }

  return (
    <div data-testid="pihole-gravity-widget" className="space-y-3 text-sm">
      {result.isPending && <p className="text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Pi-hole adlists temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}

      {result.data && (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground">
            <span>
              Gravity domains:{' '}
              <span className="font-medium text-foreground tabular-nums">
                {result.data.gravity_domains != null
                  ? result.data.gravity_domains.toLocaleString()
                  : '—'}
              </span>
            </span>
            <span>
              Last update:{' '}
              <span className="font-medium text-foreground">
                {result.data.gravity_last_update_age_seconds != null
                  ? formatAge(result.data.gravity_last_update_age_seconds)
                  : '—'}
              </span>
            </span>
          </div>

          {result.data.rows.length === 0 ? (
            <EmptyState testId="pihole-adlists-empty">No adlists configured</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="py-2 pr-3 font-medium">List</th>
                    <th className="py-2 pr-3 font-medium">Address</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 pr-3 font-medium">Enabled</th>
                    <th className="hidden py-2 pr-3 font-medium sm:table-cell">Domains</th>
                  </tr>
                </thead>
                <tbody>
                  {result.data.rows.map((row: AdlistRow) => {
                    const variant = adlistStatusToBadgeVariant(row.status)
                    const failing = variant === 'critical'
                    return (
                      <tr
                        key={`${row.list}:${row.address}`}
                        className={
                          failing
                            ? 'border-b border-red-300/60 bg-red-500/5'
                            : 'border-b border-border'
                        }
                      >
                        <td className="py-2 pr-3">{row.list}</td>
                        <td className="py-2 pr-3 font-mono break-all">{row.address}</td>
                        <td className="py-2 pr-3">
                          <Badge variant={variant}>
                            {row.status === '' ? 'unknown' : row.status}
                          </Badge>
                        </td>
                        <td className="py-2 pr-3">{row.enabled ? 'yes' : 'no'}</td>
                        <td className="hidden py-2 pr-3 tabular-nums sm:table-cell">
                          {row.domains != null ? row.domains.toLocaleString() : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div>
            <Button
              variant="default"
              onClick={() => {
                setErrorMessage('')
                setConfirmOpen(true)
              }}
              disabled={mutation.isPending}
              data-testid="pihole-update-gravity-button"
            >
              {mutation.isPending ? 'Updating gravity…' : 'Update gravity now'}
            </Button>
          </div>
        </>
      )}

      <ConfirmPhraseDialog
        open={confirmOpen}
        onOpenChange={(o) => {
          setConfirmOpen(o)
          if (!o) setErrorMessage('')
        }}
        title="Update Pi-hole gravity"
        body="This rebuilds the gravity database from all adlists. It can take ~2 minutes."
        expectedPhrase="update"
        confirmLabel="Update gravity"
        onConfirm={handleConfirm}
        isPending={mutation.isPending}
        errorMessage={errorMessage}
      />

      <Dialog open={resultOpen} onOpenChange={setResultOpen}>
        <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {resultSuccess ? 'Gravity update complete' : 'Gravity update failed'}
            </DialogTitle>
            <DialogDescription>Output from the gravity rebuild.</DialogDescription>
          </DialogHeader>
          <pre
            data-testid="pihole-gravity-log-tail"
            className="max-h-96 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-xs whitespace-pre-wrap break-all"
          >
            {logTail.length > 0 ? logTail.join('\n') : 'No output.'}
          </pre>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResultOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
