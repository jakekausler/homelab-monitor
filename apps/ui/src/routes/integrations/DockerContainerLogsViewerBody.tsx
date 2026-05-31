import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { ApiError } from '@/api/client'
import {
  dockerLogsQueryKeys,
  useContainerLogs,
  useListContainers,
  type ContainerLogsRange,
} from '@/api/docker'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { formatLogTimestamp } from '@/lib/relativeTime'
import {
  parseIso,
  resolveCustomWindow,
  toIsoZ,
  type PresetToken,
  type TimeRangeValue,
} from '@/lib/timeRange'
import type { UseLogsResult } from '@/components/logs/types'
import { StatusBadge } from '@/routes/integrations/badges'

const DEFAULT_SINCE: PresetToken = '15m'
const PRESET_TOKENS: readonly PresetToken[] = ['5m', '15m', '1h', '6h', '24h', '7d']

interface DockerContainerLogsViewerBodyProps {
  containerName: string
  since?: string | undefined
  start?: string | undefined
  end?: string | undefined
  onRangeChange?: ((next: { since?: string; start?: string; end?: string }) => void) | undefined
}

function isPresetToken(s: string): s is PresetToken {
  return (PRESET_TOKENS as readonly string[]).includes(s)
}

export function DockerContainerLogsViewerBody({
  containerName,
  since,
  start,
  end,
  onRangeChange,
}: DockerContainerLogsViewerBodyProps) {
  const [wrap, setWrap] = useState(false)
  // Bumping this re-resolves an OPEN end to a fresh "now", changing the query
  // key so Refresh extends the window to the present (live-tail groundwork).
  const [refreshNonce, setRefreshNonce] = useState(0)

  // Custom mode is active when EITHER start OR end is present in the URL; the
  // missing bound is OPEN. parseIso returns null on absent/garbage → open bound.
  const customStart = start !== undefined ? parseIso(start) : null
  const customEnd = end !== undefined ? parseIso(end) : null
  const hasCustom = customStart !== null || customEnd !== null

  const presetToken: PresetToken =
    since !== undefined && isPresetToken(since) ? since : DEFAULT_SINCE

  // For an OPEN end we resolve to "now", but the query key must stay STABLE
  // between refreshes (else every render's fresh `now` churns the key and
  // refetch-loops). So memoize the resolved window on [URL bounds, refreshNonce]:
  // `now` is only re-read when refreshNonce bumps (handleRefresh) — which is
  // exactly when we WANT the open end to extend to the present.
  const resolved = useMemo(
    () =>
      hasCustom
        ? resolveCustomWindow(
            { start: customStart ?? undefined, end: customEnd ?? undefined },
            { now: new Date(), maxSpanDays: 30 },
          )
        : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: re-resolve only on URL change or explicit refresh
    [start, end, refreshNonce],
  )

  const range: ContainerLogsRange =
    resolved !== null
      ? { start: toIsoZ(resolved.start), end: toIsoZ(resolved.end) }
      : { since: presetToken }

  // The control shows the RAW (possibly-open) URL bounds, not the resolved ones,
  // so an open end keeps reading "Now" instead of freezing to a timestamp.
  const value: TimeRangeValue = hasCustom
    ? {
        kind: 'custom',
        start: customStart ?? undefined,
        end: customEnd ?? undefined,
      }
    : { kind: 'preset', token: presetToken }

  const logs = useContainerLogs(containerName, range)
  const qc = useQueryClient()

  const containerList = useListContainers()
  const cachedRow = containerList.data?.containers.find((c) => c.name === containerName) ?? null

  const handleRefresh = () => {
    setRefreshNonce((n) => n + 1)
    void qc.invalidateQueries({ queryKey: dockerLogsQueryKeys.logs(containerName, range) })
  }

  const handleRangeChange = (v: TimeRangeValue): void => {
    if (onRangeChange === undefined) return
    if (v.kind === 'preset') {
      onRangeChange({ since: v.token })
    } else {
      const next: { since?: string; start?: string; end?: string } = {}
      if (v.start !== undefined) next.start = toIsoZ(v.start)
      if (v.end !== undefined) next.end = toIsoZ(v.end)
      onRangeChange(next)
    }
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

  const pages = logs.data?.pages ?? []
  const firstPage = pages[0]
  const flatLines = pages
    .slice()
    .reverse()
    .flatMap((p) => p.lines)

  const emptyCopy = hasCustom
    ? 'No log lines in the selected range. Try widening the time window.'
    : `No log lines in the last ${presetToken}. Try widening the time window.`

  const header = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{containerName}</span>
          {cachedRow?.status != null && <StatusBadge status={cachedRow.status} />}
          {flatLines.length > 0 && (
            <span className="text-xs text-muted-foreground" data-testid="last-log-at">
              Last: {formatLogTimestamp(flatLines[flatLines.length - 1]?.timestamp)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <WrapToggle checked={wrap} onChange={setWrap} id="docker-wrap" />
          <TimeRangeControl value={value} onChange={handleRangeChange} presets={PRESET_TOKENS} />
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
      return {
        lines: undefined,
        isLoading: false,
        isError: false,
        error: undefined,
      }
    }
    return {
      lines: flatLines,
      isLoading: logs.isLoading,
      isError: false,
      error: undefined,
      logStatus:
        firstPage?.log_status === 'no_lines'
          ? 'no_lines'
          : firstPage?.log_status === 'available'
            ? 'available'
            : undefined,
      truncated: firstPage?.truncated,
      hasMore: logs.hasNextPage,
      isLoadingOlder: logs.isFetchingNextPage,
      loadOlder: () => {
        void logs.fetchNextPage()
      },
    }
  }

  return <LogViewer useLogs={useLogs} headerSlot={header} emptyStateCopy={emptyCopy} wrap={wrap} />
}
