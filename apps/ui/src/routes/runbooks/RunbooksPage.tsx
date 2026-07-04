import { useEffect, useState } from 'react'
import type { JSX } from 'react'
import { Link } from '@tanstack/react-router'
import { AlertTriangle, RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/EmptyState'
import { cn } from '@/lib/utils'
import { useAutofixKillSwitch } from '@/api/autofixSettings'
import { useRefreshRunbooks, useRunbooks, useRunbookStats } from '@/api/runbooks'

import { PendingApprovalsPanel } from './PendingApprovalsPanel'
import { RunbookCard } from './RunbookCard'

export function RunbooksPage(): JSX.Element {
  const kill = useAutofixKillSwitch()
  const runbooks = useRunbooks()
  const stats = useRunbookStats()
  const refresh = useRefreshRunbooks()
  const [showSummaryDismissed, setShowSummaryDismissed] = useState(false)

  const killSwitchEnabled = kill.data?.enabled === true

  useEffect(() => {
    if (refresh.data === undefined) return
    // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect -- intentional reset of dismissed flag when new refresh data arrives
    setShowSummaryDismissed(false)
    const timer = setTimeout(() => {
      setShowSummaryDismissed(true)
    }, 5000)
    return () => {
      clearTimeout(timer)
    }
  }, [refresh.data])

  const showSummary = refresh.data !== undefined && !showSummaryDismissed

  return (
    <div data-testid="runbooks-page" className="space-y-6 p-4">
      {kill.data !== undefined && !killSwitchEnabled && (
        <div
          data-testid="runbooks-killswitch-banner"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <span>
              Auto-fix is disabled. Enable it in{' '}
              <Link to="/settings/autofix" className="underline">
                Settings → Auto-fix
              </Link>{' '}
              to toggle runbooks or approve runs.
            </span>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Runbooks</h1>
          <p className="text-sm text-muted-foreground">
            Auto-fix runbook catalog. Enable and configure per-runbook gates.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          data-testid="runbooks-refresh"
        >
          <RefreshCw
            className={cn('mr-2 h-4 w-4', refresh.isPending && 'animate-spin')}
            aria-hidden="true"
          />
          Refresh from disk
        </Button>
      </div>

      {showSummary && (
        <div
          data-testid="runbooks-refresh-summary"
          className="rounded-md border border-border bg-muted/40 p-3 text-sm"
        >
          <p>
            Registered {refresh.data.registered.length}, refreshed {refresh.data.refreshed.length},
            skipped {refresh.data.skipped.length}; {refresh.data.errors.length} errors
          </p>
          {refresh.data.errors.length > 0 && (
            <details className="mt-2 text-xs" data-testid="runbooks-refresh-errors">
              <summary className="cursor-pointer">Show errors</summary>
              <ul className="mt-1 space-y-1">
                {refresh.data.errors.map((e, i) => (
                  <li key={`${e.path}-${i}`}>
                    <code>{e.path}</code>: {e.message}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      {refresh.error !== null && (
        <p role="alert" className="text-sm text-destructive" data-testid="runbooks-refresh-error">
          {refresh.error.message}
        </p>
      )}

      {runbooks.isLoading && (
        <div
          data-testid="runbooks-loading"
          className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
        >
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={`runbook-skeleton-${i}`}>
              <CardHeader>
                <div className="h-4 w-32 rounded bg-muted" />
              </CardHeader>
              <CardContent>
                <div className="h-16 w-full rounded bg-muted" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {runbooks.error !== null && (
        <p role="alert" className="text-sm text-destructive" data-testid="runbooks-error">
          {runbooks.error.message}
        </p>
      )}

      {runbooks.data !== undefined && runbooks.data.items.length === 0 && (
        <EmptyState testId="runbooks-empty">
          <p>No runbooks registered.</p>
          <p className="mt-2 text-sm">
            Add a <code>runbook.yaml</code> under <code>runbooks/&lt;name&gt;/</code> on disk, then
            click <strong>Refresh from disk</strong>.
          </p>
        </EmptyState>
      )}

      {runbooks.data !== undefined && runbooks.data.items.length > 0 && (
        <div
          data-testid="runbooks-catalog"
          className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
        >
          {runbooks.data.items.map((r) => (
            <RunbookCard
              key={r.id}
              runbook={r}
              killSwitchEnabled={killSwitchEnabled}
              stats={stats.data?.items.find((s) => s.runbook_id === r.id)}
            />
          ))}
        </div>
      )}

      <PendingApprovalsPanel killSwitchEnabled={killSwitchEnabled} />
    </div>
  )
}
