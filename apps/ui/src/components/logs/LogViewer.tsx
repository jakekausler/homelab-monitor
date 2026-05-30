import { ChevronUp } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { EmptyState } from '@/components/EmptyState'
import { LogBanner } from './LogBanner'
import { LogLineList } from './LogLineList'
import type { LogViewerProps } from './types'

const DEFAULT_EMPTY = 'No log lines.'
const DEFAULT_UNAVAILABLE = 'Logs temporarily unavailable. The Refresh button still works.'

export function LogViewer({
  useLogs,
  headerSlot,
  emptyStateCopy,
  unavailableCopy,
  wrap,
}: LogViewerProps) {
  const {
    lines,
    isLoading,
    isError,
    error,
    logStatus,
    truncated,
    hasMore,
    isLoadingOlder,
    loadOlder,
  } = useLogs()

  return (
    <div className="space-y-4">
      {headerSlot}

      {isLoading && <p className="text-sm text-muted-foreground">Loading logs…</p>}

      {logStatus === 'unavailable' && (
        <div
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-800 dark:text-amber-200"
          data-testid="unavailable-banner"
          role="status"
        >
          {unavailableCopy ?? DEFAULT_UNAVAILABLE}
        </div>
      )}

      {logStatus === 'unknown' && (
        <EmptyState testId="logs-unknown">{unavailableCopy ?? 'Logs source not found.'}</EmptyState>
      )}

      {isError && logStatus !== 'unavailable' && logStatus !== 'unknown' && (
        <ErrorDisplay error={error} />
      )}

      {logStatus === 'no_lines' && (
        <EmptyState testId="no-lines">{emptyStateCopy ?? DEFAULT_EMPTY}</EmptyState>
      )}

      {logStatus === 'expired' && (
        <p className="text-sm text-muted-foreground" data-testid="expired-notice">
          Log text no longer available (past VictoriaLogs retention).
        </p>
      )}

      {(logStatus === 'available' || logStatus === 'running') && (
        <div className="space-y-2">
          {loadOlder != null && hasMore === true && (
            <LoadOlderButton onClick={loadOlder} isLoading={isLoadingOlder ?? false} />
          )}
          {logStatus === 'running' && (
            <LogBanner tone="blue" testId="running-banner">
              Run in progress — output as of now.
            </LogBanner>
          )}
          {truncated && (
            <LogBanner tone="amber" testId="truncated-banner" role="status">
              Showing first {String((lines ?? []).length)} lines. Narrow the time window to see all
              entries.
            </LogBanner>
          )}
          <LogLineList lines={lines ?? []} testId="logs-body" wrap={wrap ?? false} />
        </div>
      )}
    </div>
  )
}

function LoadOlderButton({ onClick, isLoading }: { onClick: () => void; isLoading: boolean }) {
  return (
    <div className="flex justify-center">
      <Button
        size="sm"
        variant="outline"
        onClick={onClick}
        disabled={isLoading}
        aria-busy={isLoading}
        data-testid="load-older"
      >
        <ChevronUp className={isLoading ? 'mr-1 size-4 animate-spin' : 'mr-1 size-4'} />
        {isLoading ? 'Loading…' : 'Load older'}
      </Button>
    </div>
  )
}
