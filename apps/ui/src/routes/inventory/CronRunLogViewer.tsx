import { Link, useParams } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { cronQueryKeys, useCronRunLog } from '@/api/crons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { RunStateBadge } from '@/components/crons/badges'
import { formatDuration } from '@/lib/relativeTime'

const RUN_ID_DISPLAY_PREFIX = 12

export function CronRunLogViewerPage() {
  const params = useParams({ strict: false })
  const fingerprint = params.fingerprint ?? ''
  const runId = params.run_id ?? ''
  const log = useCronRunLog(fingerprint, runId)
  const qc = useQueryClient()

  // 503 vl_unavailable: surface the soft "temporarily unavailable" banner.
  const isUnavailable = log.error instanceof ApiError && log.error.status === 503

  const handleRefresh = () => {
    void qc.invalidateQueries({ queryKey: cronQueryKeys.runLog(fingerprint, runId) })
  }

  return (
    <div className="space-y-4">
      <Link
        to="/inventory/crons/$fingerprint/runs"
        params={{ fingerprint }}
        search={{ cursor: undefined, state: undefined }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to runs
      </Link>

      {/* Sticky metadata header */}
      <header
        className="sticky top-0 z-10 -mx-4 space-y-2 border-b border-border bg-background/95 px-4 py-3 backdrop-blur"
        data-testid="run-log-header"
      >
        {log.isLoading && <p className="text-muted-foreground">Loading run log…</p>}
        {isUnavailable && (
          <p className="text-amber-700 dark:text-amber-300" role="status">
            Logs temporarily unavailable — try again shortly.
          </p>
        )}
        {log.error && !isUnavailable && (
          <p role="alert" className="text-red-600">
            {log.error.message}
          </p>
        )}
        {log.data && (
          <RunLogHeader
            data={log.data}
            runId={runId}
            onRefresh={handleRefresh}
            isRefreshing={log.isFetching}
          />
        )}
      </header>

      {/* Body */}
      {isUnavailable && (
        <div
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-800 dark:text-amber-200"
          data-testid="unavailable-banner"
        >
          The log backend is temporarily unavailable. The run still happened — its metadata will
          appear here once the backend recovers.
        </div>
      )}
      {log.data?.log_status === 'expired' && (
        <p className="text-sm text-muted-foreground" data-testid="expired-notice">
          Log text no longer available (past VictoriaLogs retention).
        </p>
      )}
      {log.data && log.data.log_status !== 'expired' && <RunLogBody data={log.data} />}
    </div>
  )
}

function RunLogHeader({
  data,
  runId,
  onRefresh,
  isRefreshing,
}: {
  data: import('@/api/types').Schema<'RunLogResponse'>
  runId: string
  onRefresh: () => void
  isRefreshing: boolean
}) {
  const flags = data.anomaly_flags.length > 0 ? data.anomaly_flags.split(',') : []
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs" title={runId}>
          {runId.length > RUN_ID_DISPLAY_PREFIX
            ? `${runId.slice(0, RUN_ID_DISPLAY_PREFIX)}…`
            : runId}
        </code>
        <RunStateBadge state={data.state} />
        <span className="text-xs text-muted-foreground">
          {formatDuration(data.duration_seconds)}
        </span>
        {data.entries.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {String(data.line_count ?? data.entries.length)} lines
          </span>
        )}
        {flags.map((f) => (
          <Badge key={f} variant="warn" data-testid="anomaly-badge">
            {f}
          </Badge>
        ))}
      </div>
      {data.log_status === 'running' && (
        <Button
          size="sm"
          variant="outline"
          onClick={onRefresh}
          disabled={isRefreshing}
          data-testid="refresh-log"
        >
          <RefreshCw className="mr-1 size-4" />
          {isRefreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      )}
    </div>
  )
}

function RunLogBody({ data }: { data: import('@/api/types').Schema<'RunLogResponse'> }) {
  return (
    <div className="space-y-2">
      {data.log_status === 'running' && (
        <p
          className="rounded-md border border-blue-500/30 bg-blue-500/10 p-2 text-xs text-blue-900 dark:text-blue-200"
          data-testid="running-banner"
        >
          Run in progress — output as of now.
        </p>
      )}
      {/* TODO: EPIC-004 STAGE-004-005 — cursor-based pagination + custom datetime range picker */}
      {data.truncated && (
        <p
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200"
          data-testid="truncated-banner"
        >
          Output truncated at {String(data.entries.length)} lines.
        </p>
      )}
      <pre
        className="overflow-x-auto rounded-md border border-border bg-muted/30 p-3 text-xs font-mono"
        data-testid="log-body"
      >
        {data.entries.length === 0 ? (
          <span className="text-muted-foreground">No log lines captured for this run.</span>
        ) : (
          data.entries.map((line, i) => (
            <div key={`${line.timestamp}-${String(i)}`}>
              <span className="text-muted-foreground">{line.timestamp}</span> {line.message}
            </div>
          ))
        )}
      </pre>
    </div>
  )
}
