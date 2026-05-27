import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { dockerLogsQueryKeys, useContainerLogs, useListContainers } from '@/api/docker'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/routes/integrations/badges'

const SINCE_PRESETS = ['5m', '15m', '1h', '6h', '24h', '7d'] as const
type SincePreset = (typeof SINCE_PRESETS)[number]
const DEFAULT_SINCE: SincePreset = '15m'

interface DockerContainerLogsViewerBodyProps {
  containerName: string
}

export function DockerContainerLogsViewerBody({
  containerName,
}: DockerContainerLogsViewerBodyProps) {
  const [since, setSince] = useState<SincePreset>(DEFAULT_SINCE)
  const logs = useContainerLogs(containerName, since)
  const qc = useQueryClient()

  const containerList = useListContainers()
  const cachedRow = containerList.data?.containers.find((c) => c.name === containerName) ?? null

  const handleRefresh = () => {
    void qc.invalidateQueries({ queryKey: dockerLogsQueryKeys.logs(containerName, since) })
  }

  const isUnknown = logs.error instanceof ApiError && logs.error.status === 404
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 503

  if (isUnknown) {
    return (
      <EmptyState testId="container-unknown">
        Container <code className="font-mono">{containerName}</code> not found.
      </EmptyState>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header with controls */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{containerName}</span>
          {cachedRow?.status != null && <StatusBadge status={cachedRow.status} />}
          {logs.data && logs.data.lines.length > 0 && (
            <span className="text-xs text-muted-foreground" data-testid="last-log-at">
              last: {logs.data.lines[logs.data.lines.length - 1]?.timestamp}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs text-muted-foreground" htmlFor="since-picker">
            since:
          </label>
          <select
            id="since-picker"
            data-testid="since-picker"
            className="rounded border border-input bg-background px-2 py-1 text-xs"
            value={since}
            onChange={(e) => setSince(e.target.value as SincePreset)}
          >
            {SINCE_PRESETS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="outline"
            onClick={handleRefresh}
            disabled={logs.isFetching}
            data-testid="refresh-logs"
          >
            <RefreshCw className="mr-1 size-4" />
            {logs.isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </div>

      {/* Body */}
      {logs.isLoading && <p className="text-sm text-muted-foreground">Loading logs…</p>}

      {isUnavailable && (
        <div
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-800 dark:text-amber-200"
          data-testid="unavailable-banner"
          role="status"
        >
          Logs temporarily unavailable. The Refresh button still works.
        </div>
      )}

      {logs.error && !isUnknown && !isUnavailable && (
        <p role="alert" className="text-sm text-red-600">
          Failed to load logs: {logs.error.message}
        </p>
      )}

      {logs.data && logs.data.log_status === 'no_lines' && (
        <EmptyState testId="no-lines">
          No log lines in the last {since}. Try widening the time window.
        </EmptyState>
      )}

      {logs.data && logs.data.log_status === 'available' && (
        <div className="space-y-2">
          {logs.data.truncated && (
            <p
              className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200"
              data-testid="truncated-banner"
              role="status"
            >
              Showing first {String(logs.data.lines.length)} lines. Narrow the time window to see
              all entries.
            </p>
          )}
          <pre
            className="overflow-x-auto rounded-md border border-border bg-muted/30 p-3 text-xs font-mono"
            data-testid="logs-body"
          >
            {logs.data.lines.map((entry, i) => (
              <div key={`${entry.timestamp}-${String(i)}`}>
                <span className="text-muted-foreground">{entry.timestamp}</span> {entry.line}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  )
}
