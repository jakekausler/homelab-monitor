import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import { dockerLogsQueryKeys, useContainerLogs, useListContainers } from '@/api/docker'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { LogViewer } from '@/components/logs/LogViewer'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { formatLogTimestamp } from '@/lib/relativeTime'
import type { UseLogsResult } from '@/components/logs/types'
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
  const [wrap, setWrap] = useState(false)
  const logs = useContainerLogs(containerName, since)
  const qc = useQueryClient()

  const containerList = useListContainers()
  const cachedRow = containerList.data?.containers.find((c) => c.name === containerName) ?? null

  const handleRefresh = () => {
    void qc.invalidateQueries({ queryKey: dockerLogsQueryKeys.logs(containerName, since) })
  }

  const isUnknown = logs.error instanceof ApiError && logs.error.status === 404
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 503
  const isGenericError = logs.error instanceof ApiError && !isUnknown && !isUnavailable

  if (isUnknown) {
    return (
      <EmptyState testId="container-unknown">
        Container <code className="font-mono">{containerName}</code> not found.
      </EmptyState>
    )
  }

  const header = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{containerName}</span>
          {cachedRow?.status != null && <StatusBadge status={cachedRow.status} />}
          {logs.data && logs.data.lines.length > 0 && (
            <span className="text-xs text-muted-foreground" data-testid="last-log-at">
              Last: {formatLogTimestamp(logs.data.lines[logs.data.lines.length - 1]?.timestamp)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <WrapToggle checked={wrap} onChange={setWrap} id="docker-wrap" />
          <label className="text-xs text-muted-foreground" htmlFor="since-picker">
            Since:
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
      {isGenericError && (
        <p role="alert" className="text-sm text-red-600">
          Failed to load logs: {logs.error?.message}
        </p>
      )}
    </>
  )

  const useLogs = (): UseLogsResult => {
    if (isUnavailable) {
      return {
        lines: undefined,
        isLoading: false,
        isError: true,
        error: logs.error instanceof ApiError ? logs.error : undefined,
        logStatus: 'unavailable',
      }
    }
    if (isGenericError) {
      // Generic error rendered in the header above; LogViewer renders nothing.
      return {
        lines: undefined,
        isLoading: false,
        isError: false,
        error: undefined,
      }
    }
    return {
      lines: logs.data?.lines,
      isLoading: logs.isLoading,
      isError: false,
      error: undefined,
      logStatus:
        logs.data?.log_status === 'no_lines'
          ? 'no_lines'
          : logs.data?.log_status === 'available'
            ? 'available'
            : undefined,
      truncated: logs.data?.truncated,
    }
  }

  return (
    <LogViewer
      useLogs={useLogs}
      headerSlot={header}
      emptyStateCopy={`No log lines in the last ${since}. Try widening the time window.`}
      wrap={wrap}
    />
  )
}
