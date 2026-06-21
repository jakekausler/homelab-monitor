// STAGE-007-023 — Shared UDM LogViewer plumbing: local time-range + refresh state, the
// useLogsQuery call (services CSV EMPTY — UDM scope lives in the expr), and the page→
// UseLogsResult adapter. Cloned from HomeAssistantLogsTab's verified adapter. The `expr`
// is built by the caller via udmLogFilters.
import { useMemo, useState } from 'react'

import { ApiError } from '@/api/client'
import { useLogsQuery } from '@/api/logs'
import { resolveCustomWindow, resolvePreset, toIsoZ, type TimeRangeValue } from '@/lib/timeRange'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import type { LogViewerStatus, UseLogsResult } from '@/components/logs/types'

export interface UdmLogViewerState {
  /** Pass to <LogViewer useLogs={...} />. */
  useLogs: () => UseLogsResult
  /** Header control state for the caller's headerSlot. */
  range: TimeRangeValue
  setRange: (v: TimeRangeValue) => void
  wrap: boolean
  setWrap: (v: boolean) => void
  timezone: 'local' | 'utc'
  toggleTimezone: () => void
  isFetching: boolean
  handleRefresh: () => void
}

/**
 * @param expr  the fully-built LogsQL expression (from udmLogFilters). The udm-* scope and
 *              any IP/category filtering are already baked into this string.
 *
 * Time-range defaults to '1h' preset.
 */
export function useUdmLogViewer(expr: string): UdmLogViewerState {
  const [wrap, setWrap] = useState(false)
  const [timezone, toggleTimezone] = useTimezonePreference()
  const [refreshNonce, setRefreshNonce] = useState(0)
  const [range, setRange] = useState<TimeRangeValue>({ kind: 'preset', token: '1h' })

  const rangeKind = range.kind
  const rangeToken = range.kind === 'preset' ? range.token : undefined
  const rangeStartTime = range.kind === 'custom' ? (range.start?.getTime() ?? null) : null
  const rangeEndTime = range.kind === 'custom' ? (range.end?.getTime() ?? null) : null

  const { startIso, endIso } = useMemo(() => {
    const now = new Date()
    const win =
      rangeKind === 'preset'
        ? resolvePreset(rangeToken!, now)
        : resolveCustomWindow(
            {
              start: rangeStartTime !== null ? new Date(rangeStartTime) : undefined,
              end: rangeEndTime !== null ? new Date(rangeEndTime) : undefined,
            },
            { now, maxSpanDays: 30 },
          )
    return { startIso: toIsoZ(win.start), endIso: toIsoZ(win.end) }
    // eslint-disable-next-line react-hooks/exhaustive-deps, @eslint-react/exhaustive-deps -- intentional: refreshNonce re-resolves the window (fresh `now`) on explicit refresh
  }, [rangeKind, rangeToken, rangeStartTime, rangeEndTime, refreshNonce])

  // services CSV intentionally EMPTY — UDM scope is in `expr` (udm-* wildcard).
  const logs = useLogsQuery(expr, startIso, endIso, '')

  const handleRefresh = (): void => {
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  const flatLines = useMemo(
    () =>
      (logs.data?.pages ?? [])
        .slice()
        .reverse()
        .flatMap((p) => p.lines),
    [logs.data],
  )
  const hasData = logs.data !== undefined

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
    if (isGenericApiError) {
      return {
        lines: [],
        isLoading: false,
        isError: false,
        error: undefined,
        logStatus: 'unavailable',
      }
    }
    const status: LogViewerStatus | undefined =
      !hasData && flatLines.length === 0
        ? undefined
        : flatLines.length === 0
          ? 'no_lines'
          : 'available'
    return {
      lines: flatLines,
      isLoading: logs.isLoading && flatLines.length === 0,
      isError: false,
      error: undefined,
      logStatus: status,
      hasMore: logs.hasNextPage,
      isLoadingOlder: logs.isFetchingNextPage,
      loadOlder: () => {
        void logs.fetchNextPage()
      },
    }
  }

  return {
    useLogs,
    range,
    setRange,
    wrap,
    setWrap,
    timezone,
    toggleTimezone,
    isFetching: logs.isFetching,
    handleRefresh,
  }
}
